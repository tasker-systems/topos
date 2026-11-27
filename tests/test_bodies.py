"""What the body parser collects from SQL and PL/pgSQL."""

from __future__ import annotations

from topos.extract.bodies import references_in_plpgsql, references_in_sql

PLPGSQL = """
CREATE FUNCTION f(a uuid) RETURNS void LANGUAGE plpgsql AS $$
DECLARE r record; n int;
BEGIN
    IF NOT s.allowed(a) THEN RAISE EXCEPTION 'no'; END IF;
    n := helper(a, 1);
    FOR r IN SELECT * FROM t WHERE id = a LOOP
        PERFORM touch(r.id);
    END LOOP;
    WITH recent AS (SELECT id FROM s.log) UPDATE u SET x = 1 WHERE y IN (SELECT id FROM recent);
    INSERT INTO s.log (id) SELECT id FROM v;
    DELETE FROM u WHERE false;
    EXECUTE 'SELECT 1';
END
$$;
"""


def test_plpgsql_reads_writes_and_calls_are_collected() -> None:
    refs = references_in_plpgsql(PLPGSQL)
    assert refs.errors == []
    assert refs.reads >= {("t",), ("s", "log"), ("v",), ("recent",)}
    assert ("u",) not in refs.reads
    assert refs.writes == {("u",): {"update", "delete"}, ("s", "log"): {"insert"}}
    assert refs.calls[("s", "allowed")] == {1}
    assert refs.calls[("helper",)] == {2}
    assert refs.calls[("touch",)] == {1}
    assert refs.local_names == {"recent"}


def test_dynamic_sql_is_counted_not_resolved() -> None:
    assert references_in_plpgsql(PLPGSQL).dynamic_sql == 1


def test_sql_body_with_several_statements() -> None:
    refs = references_in_sql("SELECT a FROM x; TRUNCATE y, s.z; CREATE TEMP TABLE w (i int)")
    assert refs.reads == {("x",)}
    assert refs.writes == {("y",): {"truncate"}, ("s", "z"): {"truncate"}}
    assert refs.local_names == {"w"}


def test_a_body_that_does_not_parse_is_reported() -> None:
    refs = references_in_sql("SELEC nothing")
    assert refs.errors
    assert not refs.reads
