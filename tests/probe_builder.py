"""The fixture builder for the synthetic probe schema in `fixtures/probe.sql`."""

from __future__ import annotations

from typing import Any

DECLARATION: dict[str, Any] = {
    "name": "synthetic-shares",
    "owner": "topos-tests",
    "statement": "A user's group role and share flags, against a note or a group.",
    "axes": [
        {
            "id": "role",
            "side": "actor",
            "from": {"enum": "public.member_role"},
            "add": [None],
            "add_reason": "no membership",
        },
        {"id": "can_read", "side": "actor", "from": {"type": "boolean"}},
        {"id": "can_edit", "side": "actor", "from": {"type": "boolean"}},
        {"id": "subject", "side": "object", "from": {"check": "public.shares.subject_table"}},
    ],
    "handles": [
        {"id": "actor", "table": "public.users"},
        {"id": "note", "table": "public.notes"},
        {"id": "group", "table": "public.groups"},
    ],
    "actor_handle": "actor",
}


def build(cursor: Any, combination: dict[str, Any], ident: Any) -> dict[str, Any]:
    user, group, note = ident("user"), ident("group"), ident("note")
    cursor.execute("INSERT INTO users VALUES (%s, 'u')", [user])
    cursor.execute("INSERT INTO groups VALUES (%s, 'g')", [group])
    cursor.execute("INSERT INTO notes VALUES (%s, %s)", [note, group])
    if combination["role"] is not None:
        cursor.execute(
            "INSERT INTO memberships VALUES (%s, %s, %s)", [group, user, combination["role"]]
        )
    subject = note if combination["subject"] == "notes" else group
    cursor.execute(
        "INSERT INTO shares VALUES (%s, %s, %s, %s, %s)",
        [combination["subject"], subject, user, combination["can_read"], combination["can_edit"]],
    )
    return {"actor": user, "note": note, "group": group}
