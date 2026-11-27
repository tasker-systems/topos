"""Read a PostgreSQL database's catalog into a plain snapshot.

The snapshot is everything the topology builder needs, as JSON-serializable data keyed by object
id. It is read from the applied schema, not from migration history, so the definition in force is
the only one seen. Objects that belong to an extension, and the system schemas, are left out and
counted, so their absence is stated rather than silent.

Reading needs `psycopg`. Building a topology from a snapshot does not, so the builder can be tested
without a database.
"""

from __future__ import annotations

from typing import Any

SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")

_NOT_SYSTEM = (
    "n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')"
    " AND n.nspname NOT LIKE 'pg_temp_%' AND n.nspname NOT LIKE 'pg_toast_temp_%'"
)


def _not_extension(catalog: str, alias: str) -> str:
    return (
        "NOT EXISTS (SELECT 1 FROM pg_depend e WHERE e.classid = "
        f"'{catalog}'::regclass AND e.objid = {alias}.oid AND e.deptype = 'e')"
    )


QUERIES: dict[str, str] = {
    "relations": f"""
        SELECT c.oid::bigint AS oid, n.nspname AS schema, c.relname AS name,
               c.relkind::text AS relkind, c.relrowsecurity AS row_security
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p', 'v', 'm') AND {_NOT_SYSTEM}
          AND {_not_extension("pg_class", "c")}
          AND NOT c.relispartition
        ORDER BY n.nspname, c.relname""",
    "columns": f"""
        SELECT a.attrelid::bigint AS rel, a.attnum AS num, a.attname AS name,
               format_type(a.atttypid, a.atttypmod) AS type, a.atttypid::bigint AS type_oid,
               NOT a.attnotnull AS nullable, pg_get_expr(d.adbin, d.adrelid) AS "default"
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attnum > 0 AND NOT a.attisdropped AND c.relkind IN ('r', 'p', 'v', 'm')
          AND {_NOT_SYSTEM}
        ORDER BY a.attrelid, a.attnum""",
    "constraints": f"""
        SELECT con.oid::bigint AS oid, con.conname AS name, con.contype::text AS kind,
               con.conrelid::bigint AS rel, con.confrelid::bigint AS ref_rel,
               con.conkey::int[] AS cols, con.confkey::int[] AS ref_cols,
               pg_get_constraintdef(con.oid) AS definition
        FROM pg_constraint con
        JOIN pg_class c ON c.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE con.contype IN ('p', 'f', 'c') AND {_NOT_SYSTEM}
        ORDER BY con.conrelid, con.conname""",
    "types": f"""
        SELECT t.oid::bigint AS oid, n.nspname AS schema, t.typname AS name,
               t.typtype::text AS typtype,
               ARRAY(SELECT e.enumlabel::text FROM pg_enum e WHERE e.enumtypid = t.oid
                     ORDER BY e.enumsortorder) AS labels
        FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE t.typtype IN ('e', 'd') AND {_NOT_SYSTEM} AND {_not_extension("pg_type", "t")}
        ORDER BY n.nspname, t.typname""",
    "functions": f"""
        SELECT p.oid::bigint AS oid, n.nspname AS schema, p.proname AS name,
               p.prokind::text AS kind,
               ARRAY(SELECT format_type(x, NULL) FROM unnest(p.proargtypes) WITH ORDINALITY
                     AS a(x, i) ORDER BY i) AS args,
               p.pronargs AS nargs, p.pronargdefaults AS nargdefaults,
               format_type(p.prorettype, NULL) AS returns, l.lanname AS language,
               p.provolatile::text AS volatility, p.prosecdef AS security_definer,
               p.prosrc AS src, p.prosqlbody IS NOT NULL AS atomic,
               CASE WHEN l.lanname IN ('sql', 'plpgsql') THEN pg_get_functiondef(p.oid) END
                 AS definition,
               coalesce(p.proconfig, ARRAY[]::text[]) AS config
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        JOIN pg_language l ON l.oid = p.prolang
        WHERE p.prokind IN ('f', 'p') AND {_NOT_SYSTEM} AND {_not_extension("pg_proc", "p")}
        ORDER BY n.nspname, p.proname, p.oid""",
    "triggers": """
        SELECT t.oid::bigint AS oid, t.tgname AS name, t.tgrelid::bigint AS rel,
               t.tgfoid::bigint AS func, t.tgtype::int AS tgtype
        FROM pg_trigger t WHERE NOT t.tgisinternal
        ORDER BY t.tgrelid, t.tgname""",
    "policies": """
        SELECT p.oid::bigint AS oid, p.polname AS name, p.polrelid::bigint AS rel,
               p.polcmd::text AS cmd, p.polpermissive AS permissive,
               ARRAY(SELECT CASE WHEN r = 0 THEN 'public' ELSE r::regrole::text END
                     FROM unnest(p.polroles) AS r ORDER BY 1) AS roles
        FROM pg_policy p
        ORDER BY p.polrelid, p.polname""",
    # What views, policies, column defaults and check constraints reference, as the server
    # resolved it when they were created. This is exact, so their bodies are not parsed.
    "depends": """
        SELECT 'view' AS via, r.ev_class::bigint AS owner, d.refclassid::regclass::text AS ref_class,
               d.refobjid::bigint AS ref, d.refobjsubid AS ref_sub
        FROM pg_depend d JOIN pg_rewrite r ON d.classid = 'pg_rewrite'::regclass AND d.objid = r.oid
        WHERE d.refobjid <> r.ev_class AND d.deptype = 'n'
        UNION ALL
        SELECT 'policy', p.oid::bigint, d.refclassid::regclass::text, d.refobjid::bigint,
               d.refobjsubid
        FROM pg_depend d JOIN pg_policy p ON d.classid = 'pg_policy'::regclass AND d.objid = p.oid
        WHERE d.refobjid <> p.polrelid AND d.deptype = 'n'
        UNION ALL
        SELECT 'default', a.adrelid::bigint, d.refclassid::regclass::text, d.refobjid::bigint,
               a.adnum
        FROM pg_depend d JOIN pg_attrdef a ON d.classid = 'pg_attrdef'::regclass AND d.objid = a.oid
        WHERE d.refobjid <> a.adrelid AND d.deptype = 'n'
        UNION ALL
        SELECT 'check', c.conrelid::bigint, d.refclassid::regclass::text, d.refobjid::bigint,
               d.refobjsubid
        FROM pg_depend d JOIN pg_constraint c
          ON d.classid = 'pg_constraint'::regclass AND d.objid = c.oid
        WHERE c.contype = 'c' AND d.refobjid <> c.conrelid AND d.deptype = 'n'
        ORDER BY 1, 2, 3, 4, 5""",
    "extensions": """
        SELECT x.extname AS name, x.extversion AS version,
               (SELECT count(*) FROM pg_depend e WHERE e.refclassid = 'pg_extension'::regclass
                  AND e.refobjid = x.oid AND e.deptype = 'e')::int AS members
        FROM pg_extension x ORDER BY x.extname""",
    "catalog_function_names": """
        SELECT DISTINCT p.proname AS name FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'pg_catalog' ORDER BY 1""",
    "extension_object_names": """
        SELECT DISTINCT n.nspname AS schema, p.proname AS name, 'function' AS kind
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        JOIN pg_depend e ON e.classid = 'pg_proc'::regclass AND e.objid = p.oid
          AND e.deptype = 'e'
        UNION
        SELECT DISTINCT n.nspname, c.relname, 'relation'
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_depend e ON e.classid = 'pg_class'::regclass AND e.objid = c.oid
          AND e.deptype = 'e'
        ORDER BY 1, 2, 3""",
}


def read_catalog(dsn: str) -> dict[str, Any]:
    """Read the catalog of the database at `dsn` into a snapshot. The connection is read-only."""
    # Imported here: only reading needs the driver, so the builder runs without it.
    import psycopg
    from psycopg.rows import dict_row

    snapshot: dict[str, Any] = {}
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        connection.read_only = True
        with connection.cursor() as cursor:
            cursor.execute("SHOW server_version")
            snapshot["server_version"] = cursor.fetchone()["server_version"]
            cursor.execute("SHOW search_path")
            snapshot["search_path"] = cursor.fetchone()["search_path"]
            for name, query in QUERIES.items():
                cursor.execute(query)
                snapshot[name] = [dict(row) for row in cursor.fetchall()]
        connection.rollback()
    return snapshot
