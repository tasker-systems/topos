"""Qualify an observed model against a declared intent, and emit a findings report.

    python3 -m topos.qualify --model model.json [--intent intent.json --intent-ref REF] \
        [--area area.json] --out findings.json

This is the qualify stage. It reads a model and an intent, and never the target. Every access fact
lands in exactly one category:

- **unqualified:** no rule's access selector matches it. It is reported, never dropped, and never
  read as intended.
- **contrary to intent:** a matching rule is violated. A `never` rule is violated by an allowed
  fact (`reachable-not-intended`); a `may` rule by a denied one (`intended-not-reachable`). There
  is one finding per violated rule. If the fact matches both a `may` and a `never` rule, each of
  its findings lists every matching rule under `conflicting_rules`.
- **intended:** every matching rule is satisfied.

A rule whose selector matches no access fact is reported once as `intended-not-reachable`, with an
empty witness path. `indeterminate` entries (errors and NULLs) are counted per action and outcome;
they never satisfy or violate a rule. `not_constructed` entries are counted in
`examined.not_examined`. With no intent, every fact is unqualified and nothing is called a risk.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from topos.canonical import sha256

NO_MATCH = "no access fact matches the rule's selector"


def _selector(rule: dict[str, Any]) -> dict[str, Any] | None:
    return rule.get("concerns", {}).get("access")


def _matches(
    selector: dict[str, Any], fact: dict[str, Any], actor_axes: dict[str, dict[str, Any]]
) -> bool:
    if "actions" in selector and fact["action"] not in selector["actions"]:
        return False
    if "object_classes" in selector and fact["object_class"] not in selector["object_classes"]:
        return False
    axes = actor_axes.get(fact["actor_class"], {})
    for axis, accepted in selector.get("actor_axes", {}).items():
        if axis not in axes or axes[axis] not in accepted:
            return False
    return True


def _persona(rule: dict[str, Any] | None, actor_class: str) -> str:
    if rule is None:
        return actor_class
    applies = rule.get("applies_to", {})
    named = applies.get("personas") or applies.get("roles") or []
    return ",".join(named) if named else actor_class


def _evidence(fact: dict[str, Any], actor: dict[str, Any] | None) -> dict[str, Any]:
    inferred = [
        element["id"]
        for element in (fact, actor)
        if element is not None and element["provenance"]["kind"] == "inferred"
    ]
    if inferred:
        return {"all_extracted_or_probed": False, "inferred_elements": inferred}
    return {"all_extracted_or_probed": True}


def qualify(
    model: dict[str, Any],
    intent: dict[str, Any] | None,
    *,
    intent_ref: str | None = None,
    area: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The findings report for `model` under `intent`, or under no intent when it is None."""
    actors = {actor["id"]: actor for actor in model.get("actor_classes", [])}
    actor_axes = {key: actor["axes"] for key, actor in actors.items()}
    rules = list(intent["rules"]) if intent else []
    access_rules = [(rule, _selector(rule)) for rule in rules if _selector(rule) is not None]

    findings: list[dict[str, Any]] = []
    matched_rules: set[str] = set()

    def add(finding: dict[str, Any]) -> None:
        findings.append({"id": f"f{len(findings) + 1:07d}", **finding})

    for fact in model.get("access", []):
        matching = [rule for rule, selector in access_rules if _matches(selector, fact, actor_axes)]
        matched_rules.update(rule["id"] for rule in matching)
        common = {
            "actor_class": fact["actor_class"],
            "action": fact["action"],
            "object_class": fact["object_class"],
            "witness": {"access_fact": fact["id"]},
            "evidence": _evidence(fact, actors.get(fact["actor_class"])),
        }
        if not matching:
            add(
                {
                    "kind": "unqualified",
                    "persona": _persona(None, fact["actor_class"]),
                    "rule": None,
                    **common,
                }
            )
            continue
        polarities = {rule["polarity"] for rule in matching}
        conflict = [rule["id"] for rule in matching] if len(polarities) > 1 else None
        violated = [rule for rule in matching if (rule["polarity"] == "may") != fact["allowed"]]
        for rule in violated:
            finding = {
                "kind": "reachable-not-intended" if fact["allowed"] else "intended-not-reachable",
                "persona": _persona(rule, fact["actor_class"]),
                "rule": rule["id"],
                **common,
            }
            if conflict:
                finding["conflicting_rules"] = conflict
            add(finding)
        if not violated:
            rule = matching[0]
            add(
                {
                    "kind": "intended",
                    "persona": _persona(rule, fact["actor_class"]),
                    "rule": rule["id"],
                    **common,
                }
            )

    indeterminate_by: dict[str, Counter[str]] = defaultdict(Counter)
    for entry in model.get("indeterminate", []):
        indeterminate_by[f"{entry['action']}\x1f{entry['outcome']}"][entry["detail"]] += 1
    for rule, selector in access_rules:
        if rule["id"] in matched_rules:
            continue
        blocked = NO_MATCH
        unanswered = sum(
            1 for entry in model.get("indeterminate", []) if _matches(selector, entry, actor_axes)
        )
        if unanswered:
            blocked += f"; {unanswered} indeterminate entries match it, and they answer nothing"
        add(
            {
                "kind": "intended-not-reachable",
                "persona": _persona(rule, "any"),
                "rule": rule["id"],
                "witness": {"path": [], "blocked_at": blocked},
                "evidence": {"all_extracted_or_probed": True},
            }
        )

    not_constructed = Counter(
        (entry.get("action", "(combination)"), entry["reason"])
        for entry in model.get("not_constructed", [])
    )
    not_examined = [
        f"not constructed: {action}: {reason} ({count})"
        for (action, reason), count in sorted(not_constructed.items())
    ]
    not_examined.extend(
        f"rule {rule['id']}: it has no access selector, so no access fact was qualified under it"
        for rule in rules
        if _selector(rule) is None
    )
    bounds: dict[str, set[str]] = defaultdict(set)
    for actor in actors.values():
        for axis, value in actor["axes"].items():
            bounds[axis].add(json.dumps(value))
    examined: dict[str, Any] = {
        "readings": ["access relation, as probed"],
        "personas": [persona["id"] for persona in intent["personas"]] if intent else [],
        "not_examined": not_examined,
        "bounds": [
            f"{axis}: {', '.join(sorted(values))}" for axis, values in sorted(bounds.items())
        ],
    }
    named = model.get("area_of_concern")
    if named:
        if area is None or sha256(area) != named["sha256"]:
            raise ValueError("the model names an area of concern; pass that area document")
        examined["area_of_concern"] = named["name"]
        examined["unexamined_crossings"] = sorted(entry["edge"] for entry in area["boundary"])

    by_kind = Counter(finding["kind"] for finding in findings)
    by_action: dict[str, Counter[str]] = defaultdict(Counter)
    for finding in findings:
        by_action[finding.get("action", "(rule)")][finding["kind"]] += 1
    indeterminate = [
        {
            "action": key.split("\x1f")[0],
            "outcome": key.split("\x1f")[1],
            "count": sum(details.values()),
            "details": dict(sorted(details.items())),
        }
        for key, details in sorted(indeterminate_by.items())
    ]
    report: dict[str, Any] = {
        "schema_version": "0.2",
        "target": {"name": model["target"]["name"], "revision": model["target"]["revision"]},
        "model": {"sha256": sha256(model)},
        "abstraction": model["abstraction"]["statement"],
        "intent": (intent_ref or f"sha256:{sha256(intent)}") if intent else None,
        "examined": examined,
        "findings": findings,
        "indeterminate": indeterminate,
        "summary": {
            "facts": len(model.get("access", [])),
            "by_kind": dict(sorted(by_kind.items())),
            "by_action": {k: dict(sorted(v.items())) for k, v in sorted(by_action.items())},
            "not_constructed": len(model.get("not_constructed", [])),
            "indeterminate": len(model.get("indeterminate", [])),
        },
    }
    document = model.get("abstraction", {}).get("document")
    if document:
        report["model"]["name"] = f"observed under abstraction {document['name']}"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--intent", type=Path, help="omit to qualify under no intent")
    parser.add_argument("--intent-ref", help="how the findings cite the intent")
    parser.add_argument("--area", type=Path, help="the area the model names, if it names one")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    def load(path: Path | None) -> dict[str, Any] | None:
        return json.loads(path.read_text(encoding="utf-8")) if path else None

    report = qualify(
        load(args.model) or {},
        load(args.intent),
        intent_ref=args.intent_ref,
        area=load(args.area),
    )
    text = json.dumps(report, indent=1, ensure_ascii=False) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
