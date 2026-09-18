"""Installation smoke tests: extension installs and exposes public objects."""

import psycopg

from conftest import DSN, _install_tasks, sql_default_version


def _pg_tasks_version(cur):
    cur.execute(
        """
        SELECT version
        FROM pg_available_extension_versions
        WHERE name = 'pg_tasks' AND installed
        """
    )
    return cur.fetchone()[0]


def _catalog_signature(cur):
    """Return a stable description of the objects in the `tasks` schema."""
    cur.execute(
        """
        WITH functions AS (
            SELECT 'function'::text AS kind,
                   p.proname::text AS name,
                   pg_get_function_identity_arguments(p.oid) AS args,
                   md5(p.prosrc) AS signature
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'tasks' AND p.prokind = 'f'
        ),
        views AS (
            SELECT 'view'::text AS kind,
                   c.relname::text AS name,
                   ''::text AS args,
                   md5(pg_get_viewdef(c.oid)) AS signature
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'tasks' AND c.relkind = 'v'
        ),
        tables AS (
            SELECT 'table'::text AS kind,
                   c.relname::text AS name,
                   ''::text AS args,
                   md5(string_agg(
                       a.attname || ':' ||
                       format_type(a.atttypid, a.atttypmod) || ':' ||
                       a.attnotnull || ':' ||
                       COALESCE(pg_get_expr(d.adbin, d.adrelid), ''),
                       ',' ORDER BY a.attnum
                   )) AS signature
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_attribute a
                   ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
            LEFT JOIN pg_attrdef d
                   ON d.adrelid = c.oid AND d.adnum = a.attnum
            WHERE n.nspname = 'tasks' AND c.relkind = 'r'
            GROUP BY c.oid, c.relname
        )
        SELECT kind, name, args, signature FROM functions
        UNION ALL SELECT kind, name, args, signature FROM views
        UNION ALL SELECT kind, name, args, signature FROM tables
        ORDER BY kind, name, args
        """
    )
    return cur.fetchall()


def test_extension_schema_and_version(tasks_db_extension):
    with tasks_db_extension.cursor() as cur:
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'pg_tasks'")
        assert cur.fetchone()[0] == "1.2"
        assert _pg_tasks_version(cur) == "1.2"
        assert _pg_tasks_version(cur) == sql_default_version()

        cur.execute(
            "SELECT extnamespace::regnamespace::text FROM pg_extension WHERE extname = 'pg_tasks'"
        )
        assert cur.fetchone()[0] == "tasks"


def test_sql_install_does_not_require_extension(tasks_db_sql):
    with tasks_db_sql.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_extension WHERE extname = 'pg_tasks'")
        assert cur.fetchone()[0] == 0


def test_sql_install_matches_extension_catalog():
    """Flat pg_tasks.sql must produce the same objects as the migration path."""
    sql_conn = psycopg.connect(DSN, autocommit=True)
    ext_conn = psycopg.connect(DSN, autocommit=True)
    try:
        with sql_conn.cursor() as cur:
            _install_tasks(cur, "sql")
            sql_sig = _catalog_signature(cur)

        with ext_conn.cursor() as cur:
            _install_tasks(cur, "extension")
            ext_sig = _catalog_signature(cur)
    finally:
        with sql_conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS tasks CASCADE")
        with ext_conn.cursor() as cur:
            cur.execute("DROP EXTENSION IF EXISTS pg_tasks CASCADE")
            cur.execute("DROP SCHEMA IF EXISTS tasks CASCADE")
        sql_conn.close()
        ext_conn.close()

    assert sql_sig == ext_sig


def test_public_objects_exist(tasks_db):
    with tasks_db.cursor() as cur:
        for obj in ("tasks.template", "tasks.types", "tasks.last_notifications"):
            cur.execute("SELECT to_regclass(%s)", (obj,))
            assert cur.fetchone()[0] is not None, f"{obj} must exist"

        for func in (
            "tasks.register(text,text,interval,integer,interval,interval)",
            "tasks.unregister(text,text)",
            "tasks.create_stats_view()",
            "tasks.notify_workers()",
            "tasks.notify_workers_throttled()",
        ):
            cur.execute(
                "SELECT to_regprocedure(%s) IS NOT NULL OR to_regclass(%s) IS NOT NULL",
                (func, func),
            )
            assert cur.fetchone()[0], f"{func} must exist"


def test_template_columns(tasks_db):
    with tasks_db.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'tasks' AND table_name = 'template'
            ORDER BY ordinal_position
            """
        )
        rows = cur.fetchall()
    cols = {name: (type_, nullable) for name, type_, nullable in rows}
    assert set(cols) == {
        "id",
        "created_at",
        "planned_to",
        "started_at",
        "finished_at",
        "execution_count",
        "canceled",
        "trace_id",
        "comment",
    }
    assert cols["id"][0] == "integer"

    # planned_to has a default but is NOT NOT NULL in the template.
    with tasks_db.cursor() as cur:
        cur.execute(
            """
            SELECT column_default IS NOT NULL
            FROM information_schema.columns
            WHERE table_schema = 'tasks' AND table_name = 'template'
              AND column_name = 'planned_to'
            """
        )
        assert cur.fetchone()[0] is True
