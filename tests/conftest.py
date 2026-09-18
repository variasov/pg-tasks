"""Shared pytest fixtures and helpers for the pg-tasks suite."""

import os
from pathlib import Path

import psycopg
import pytest

DSN = os.environ.get("PG_TASKS_DSN", "postgresql://tester:pg_tasks@localhost:5433/pg_tasks")

PG_TASKS_SQL = Path(__file__).resolve().parent.parent / "pg_tasks" / "pg_tasks.sql"


def _install_tasks(cur, method):
    """Install pg_tasks into a fresh `tasks` schema.

    `extension` exercises the CREATE EXTENSION / migration path;
    `sql` exercises the flat pg_tasks.sql installation path.
    """
    cur.execute("DROP SCHEMA IF EXISTS tasks CASCADE")
    cur.execute("CREATE SCHEMA tasks")
    if method == "extension":
        cur.execute("CREATE EXTENSION pg_tasks SCHEMA tasks")
    elif method == "sql":
        cur.execute(PG_TASKS_SQL.read_text(encoding="utf-8"))
    else:
        raise ValueError(f"unknown install method: {method}")


@pytest.fixture
def db():
    """A connection with autocommit off; each test's changes are rolled back."""
    conn = psycopg.connect(DSN)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture(params=["extension", "sql"], ids=["extension", "sql"])
def tasks_db(db, request):
    """Connection with pg_tasks installed in schema `tasks` via either method."""
    with db.cursor() as cur:
        _install_tasks(cur, request.param)
    return db


@pytest.fixture
def tasks_db_extension(db):
    """Connection with pg_tasks installed via CREATE EXTENSION only."""
    with db.cursor() as cur:
        _install_tasks(cur, "extension")
    return db


@pytest.fixture
def tasks_db_sql(db):
    """Connection with pg_tasks installed via the flat pg_tasks.sql only."""
    with db.cursor() as cur:
        _install_tasks(cur, "sql")
    return db


@pytest.fixture(params=["extension", "sql"], ids=["extension", "sql"])
def committed_db(request):
    """Autocommit connection with pg_tasks installed, visible to other sessions.

    Needed for tests involving two concurrent sessions (e.g. SKIP LOCKED),
    because uncommitted DDL is invisible to a second connection.
    """
    conn = psycopg.connect(DSN, autocommit=True)
    with conn.cursor() as cur:
        _install_tasks(cur, request.param)
    yield conn
    with conn.cursor() as cur:
        cur.execute("DROP EXTENSION IF EXISTS pg_tasks CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS tasks CASCADE")
    conn.close()


def create_task_table(db, table_name, extra_columns="payload text"):
    """Create a user task table from tasks.template."""
    with db.cursor() as cur:
        cur.execute(
            "CREATE TABLE tasks.{0} ("
            "LIKE tasks.template INCLUDING DEFAULTS "
            "INCLUDING IDENTITY INCLUDING INDEXES, {1})".format(table_name, extra_columns)
        )


def register_type(db, table_name, **params):
    """Call tasks.register() for the given table with only requested params.

    Params not provided fall back to the extension defaults.
    """
    allowed = {"timeout", "max_retries", "notification_interval", "storage_time"}
    unknown = set(params) - allowed
    if unknown:
        raise ValueError(f"unknown register params: {sorted(unknown)}")

    args = ["schema_name := %(schema)s", "table_name := %(table)s"]
    values = {"schema": "tasks", "table": table_name}

    ordering = ["timeout", "max_retries", "notification_interval", "storage_time"]
    for key in ordering:
        if key in params:
            args.append(f"{key} := %({key})s")
            values[key] = params[key]

    sql = "SELECT tasks.register({0})".format(", ".join(args))
    with db.cursor() as cur:
        cur.execute(sql, values)


def insert_task(db, table_name, payload=None, **cols):
    """Insert a task row, returning its id."""
    with db.cursor() as cur:
        values = dict(cols)
        if payload is not None:
            values["payload"] = payload
        columns = ", ".join(values.keys())
        placeholders = ", ".join("%({0})s".format(c) for c in values)
        cur.execute(
            f"INSERT INTO tasks.{table_name} ({columns}) "
            f"VALUES ({placeholders}) RETURNING id",
            values,
        )
        return cur.fetchone()[0]


def get_task(db, table_name, task_id, columns="*"):
    with db.cursor() as cur:
        cur.execute(f"SELECT {columns} FROM tasks.{table_name} WHERE id = %s", (task_id,))
        return cur.fetchone()