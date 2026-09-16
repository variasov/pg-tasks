"""Registration and unregistration of task types."""

from datetime import timedelta

import psycopg
import pytest

from conftest import create_task_table, insert_task, register_type


def test_register_creates_registry_row(tasks_db):
    """timeout/max_retries/storage_time are persisted; no live notification yet."""
    create_task_table(tasks_db, "job1")
    register_type(tasks_db, "job1", timeout="00:01:00", max_retries=3,
                  notification_interval="2s", storage_time="24:00:00")

    with tasks_db.cursor() as cur:
        cur.execute(
            """
            SELECT timeout, max_retries, storage_time
            FROM tasks.types
            WHERE schema_name = 'tasks' AND table_name = 'job1'
            """
        )
        assert cur.fetchone() == (timedelta(minutes=1), 3, timedelta(days=1))


@pytest.mark.xfail(
    reason="register() INSERT omits notification_interval, so the thick config is lost",
    strict=False,
)
def test_register_persists_notification_interval(tasks_db):
    create_task_table(tasks_db, "job_ni")
    register_type(tasks_db, "job_ni", notification_interval="2s")

    with tasks_db.cursor() as cur:
        cur.execute(
            "SELECT notification_interval FROM tasks.types "
            "WHERE schema_name = 'tasks' AND table_name = 'job_ni'"
        )
        assert cur.fetchone()[0] == timedelta(seconds=2)


@pytest.mark.xfail(
    reason="register() formats NULL max_retries via %s, generating broken acquire SQL",
    strict=False,
)
def test_register_defaults(tasks_db):
    create_task_table(tasks_db, "job_defaults")
    register_type(tasks_db, "job_defaults")

    with tasks_db.cursor() as cur:
        cur.execute(
            "SELECT timeout, max_retries, notification_interval, storage_time "
            "FROM tasks.types WHERE table_name = 'job_defaults'"
        )
        assert cur.fetchone() == (None, None, timedelta(seconds=1), None)


def test_register_generates_workers(tasks_db):
    create_task_table(tasks_db, "job2")
    register_type(tasks_db, "job2", max_retries=1, timeout="00:01:00")

    with tasks_db.cursor() as cur:
        cur.execute(
            """
            SELECT proname
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'tasks'
              AND proname IN (
                  'acquire_tasks_job2',
                  'cancel_tasks_job2',
                  'clean_tasks_job2',
                  'finish_tasks_job2',
                  'reset_tasks_job2',
                  'retry_tasks_job2'
              )
            ORDER BY proname
            """
        )
        assert [r[0] for r in cur.fetchall()] == [
            "acquire_tasks_job2",
            "cancel_tasks_job2",
            "clean_tasks_job2",
            "finish_tasks_job2",
            "reset_tasks_job2",
            "retry_tasks_job2",
        ]

        cur.execute("SELECT 1 FROM tasks.stats_of_tasks_job2")
        assert cur.fetchone()[0] == 1


def test_register_replaces_previous(tasks_db):
    create_task_table(tasks_db, "job3")
    register_type(tasks_db, "job3", max_retries=1, timeout="00:01:00")
    register_type(tasks_db, "job3", max_retries=7, timeout="00:02:00")

    with tasks_db.cursor() as cur:
        cur.execute(
            "SELECT count(*), min(max_retries), max(max_retries) "
            "FROM tasks.types WHERE table_name = 'job3'"
        )
        assert cur.fetchone() == (1, 7, 7)


def test_unregister_idempotent(tasks_db):
    create_task_table(tasks_db, "job4")
    register_type(tasks_db, "job4", max_retries=1, timeout="00:01:00")

    with tasks_db.cursor() as cur:
        cur.execute("SELECT tasks.unregister('tasks', 'job4')")
        cur.execute("SELECT tasks.unregister('tasks', 'job4')")
        cur.execute("SELECT count(*) FROM tasks.types WHERE table_name = 'job4'")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT to_regprocedure('tasks.acquire_tasks_job4(int)')")
        assert cur.fetchone()[0] is None


def test_unregister_after_live_notification(tasks_db):
    create_task_table(tasks_db, "job5")
    register_type(tasks_db, "job5", max_retries=1, timeout="00:01:00")
    insert_task(tasks_db, "job5", payload="a")

    with tasks_db.cursor() as cur:
        cur.execute("SELECT tasks.unregister('tasks', 'job5')")
        cur.execute("SELECT count(*) FROM tasks.types WHERE table_name = 'job5'")
        assert cur.fetchone()[0] == 0