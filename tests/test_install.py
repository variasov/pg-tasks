"""Installation smoke tests: extension installs and exposes public objects."""


def test_extension_schema_and_version(tasks_db):
    with tasks_db.cursor() as cur:
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'pg_tasks'")
        assert cur.fetchone()[0] == "1.3"

        cur.execute("SELECT extnamespace::regnamespace::text FROM pg_extension WHERE extname = 'pg_tasks'")
        assert cur.fetchone()[0] == "tasks"


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
            cur.execute("SELECT to_regprocedure(%s) IS NOT NULL OR to_regclass(%s) IS NOT NULL", (func, func))
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
        "id", "created_at", "planned_to", "started_at", "finished_at",
        "execution_count", "canceled", "trace_id", "comment",
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