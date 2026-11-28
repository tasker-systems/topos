"""A PostgreSQL target for the probe engine.

Each combination is built and probed inside one transaction that is always rolled back, so a probe
leaves its database as it found it. The fixture builder runs inside a savepoint: if the database
refuses what it writes (a check constraint, a trigger), the combination is recorded as
unconstructible with the database's own message. Each call runs in its own savepoint, which is always
rolled back: an error answers that call alone and is recorded as an error, never as "denied", and
no call sees what another wrote. A call to a volatile function is checked for writes by comparing
the transaction's row-change counters before and after it.

A fixture builder is a function

    build(cursor, combination, ident) -> dict[str, Any] | Unconstructible

`cursor` is a psycopg cursor inside the combination's transaction. `combination` maps each axis id
to its value. `ident(name)` returns a UUID determined by the combination and `name`, so the same
combination always gets the same row ids and a probe's calls are reproducible. The builder returns
the value of each handle the abstraction declares, or `Unconstructible(reason)`.

Run a probe with:

    python3 -m topos.probe.postgres --dsn postgresql://... --abstraction abstraction.json \
        --builder path/to/builder.py:build --revision SHA --out model.json
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from topos.canonical import sha256
from topos.probe.engine import Call, Fixture, Outcome, Unconstructible, observe

NAMESPACE = uuid.UUID("5b0a4f0e-6f3c-4c0e-9a57-7a6f70f05e00")

Builder = Callable[[Any, dict[str, Any], Callable[[str], uuid.UUID]], Any]


class PostgresTarget:
    """Builds fixtures with a builder callback and calls predicate functions.

    The connection must be in autocommit mode, so each combination's transaction is its own.
    """

    def __init__(self, connection: Any, builder: Builder, *, statement_timeout_ms: int = 10000):
        self.connection = connection
        self.builder = builder
        self.statement_timeout_ms = statement_timeout_ms
        self._cursor: Any = None

    @contextmanager
    def combination(self, combination: dict[str, Any]) -> Iterator[Fixture | Unconstructible]:
        import psycopg

        key = sha256(combination)

        def ident(name: str) -> uuid.UUID:
            return uuid.uuid5(NAMESPACE, f"{key}/{name}")

        try:
            with self.connection.transaction() as outer, self.connection.cursor() as cursor:
                cursor.execute(f"SET LOCAL statement_timeout = {int(self.statement_timeout_ms)}")
                self._cursor = cursor
                built: Fixture | Unconstructible
                try:
                    with self.connection.transaction():
                        result = self.builder(cursor, combination, ident)
                    if isinstance(result, Unconstructible):
                        built = result
                    else:
                        built = Fixture({k: _plain(v) for k, v in result.items()})
                except psycopg.Error as error:
                    built = Unconstructible(_describe(error))
                yield built
                raise psycopg.Rollback(outer)
        finally:
            self._cursor = None

    def call(self, call: Call, fixture: Fixture) -> Outcome:
        import psycopg

        function = call.predicate["function"]
        placeholders: list[str] = []
        values: list[Any] = []
        for argument in call.arguments:
            if argument.default:
                continue
            value = fixture.handles[argument.handle] if argument.handle else argument.value
            placeholders.append(f"%s::{argument.type}")
            values.append(_plain(value))
        name = f'"{function["schema"]}"."{function["name"]}"'
        sql = f"SELECT {name}({', '.join(placeholders)})"
        check = call.predicate.get("volatility") == "volatile"
        wrote: bool | None = None
        try:
            # The savepoint is always rolled back, so no call sees another's writes.
            with self.connection.transaction() as savepoint:
                before = self._changes() if check else 0
                self._cursor.execute(sql, values)
                answer = self._cursor.fetchone()[0]
                if check:
                    wrote = self._changes() != before
                raise psycopg.Rollback(savepoint)
        except psycopg.Error as error:
            return Outcome("error", _describe(error), wrote)
        if answer is True:
            return Outcome("true", wrote=wrote)
        if answer is False:
            return Outcome("false", wrote=wrote)
        if answer is None:
            return Outcome("null", "the function returned NULL", wrote)
        return Outcome("error", f"the function returned a non-boolean {answer!r}", wrote)

    def _changes(self) -> int:
        """Rows inserted, updated or deleted so far in this transaction, over every table."""
        self._cursor.execute(
            "SELECT coalesce(sum(n_tup_ins + n_tup_upd + n_tup_del), 0)::bigint"
            " FROM pg_stat_xact_all_tables"
        )
        return int(self._cursor.fetchone()[0])


def _plain(value: Any) -> Any:
    return str(value) if isinstance(value, uuid.UUID) else value


def _describe(error: Any) -> str:
    diag = getattr(error, "diag", None)
    state = getattr(diag, "sqlstate", None) or "?"
    message = getattr(diag, "message_primary", None) or str(error).splitlines()[0]
    return f"{state}: {message}"


def load_builder(spec: str) -> tuple[Builder, str]:
    """Load `path.py:function`, and return it with `path.py@sha256:<hex>` naming its source."""
    path_text, _, attribute = spec.rpartition(":")
    path = Path(path_text)
    source = path.read_bytes()
    module_spec = importlib.util.spec_from_file_location(f"_topos_builder_{path.stem}", path)
    if module_spec is None or module_spec.loader is None:
        raise SystemExit(f"cannot load a builder from {path}")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return getattr(module, attribute), f"{path.name}@sha256:{hashlib.sha256(source).hexdigest()}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--abstraction", required=True, type=Path)
    parser.add_argument("--builder", required=True, help="path/to/builder.py:function")
    parser.add_argument("--revision", required=True, help="the target revision the database holds")
    parser.add_argument("--statement-timeout-ms", type=int, default=10000)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    import psycopg

    abstraction = json.loads(args.abstraction.read_text(encoding="utf-8"))
    builder, builder_ref = load_builder(args.builder)
    with psycopg.connect(args.dsn, autocommit=True) as connection:
        target = PostgresTarget(connection, builder, statement_timeout_ms=args.statement_timeout_ms)
        model = observe(abstraction, target, revision=args.revision, builder=builder_ref)
    text = json.dumps(model, indent=1, ensure_ascii=False) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
