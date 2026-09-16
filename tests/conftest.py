"""Shared pytest fixtures and helpers for the pg-tasks suite."""

import os

import psycopg
import pytest

DSN = os.environ.get("PG_TASKS_DSN", "postgresql://tester:pg_tasks@localhost:5433/pg_tasks")


@pytest.fixture
def db():
    """A connection with autocommit off; each test's changes are rolled back."""
    conn = psycopg.connect(DSN)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture
def tasks_db(db):
    """Connection with a fresh pg_tasks extension installed in schema `tasks`."""
    with db.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS tasks CASCADE")
        cur.execute("CREATE SCHEMA tasks")
        cur.execute("CREATE EXTENSION pg_tasks SCHEMA tasks")
    return db


@pytest.fixture
def committed_db():
    """Autocommit connection with the extension installed, visible to other sessions.

    Needed for tests involving two concurrent sessions (e.g. SKIP LOCKED),
    because uncommitted DDL is invisible to a second connection.
    """
    conn = psycopg.connect(DSN, autocommit=True)
    with conn.cursor() as cur:
        cur.execute("DROP EXTENSION IF EXISTS pg_tasks CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS tasks CASCADE")
        cur.execute("CREATE SCHEMA tasks")
        cur.execute("CREATE EXTENSION pg_tasks SCHEMA tasks")
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