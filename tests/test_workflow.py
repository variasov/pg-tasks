"""Acquire, finish, retry, cancel, reset, clean workflows."""

import psycopg
import pytest

from conftest import DSN, create_task_table, insert_task, register_type


@pytest.fixture
def jobs(tasks_db):
    create_task_table(tasks_db, "jobs")
    register_type(tasks_db, "jobs", timeout="00:05:00", max_retries=3)
    return tasks_db


def acquired(db, n=1, table="jobs"):
    with db.cursor() as cur:
        cur.execute(f"SELECT id, execution_count FROM tasks.acquire_tasks_{table}({n})")
        return cur.fetchall()


def test_acquire_takes_available_tasks(jobs):
    id1 = insert_task(jobs, "jobs", payload="a")
    id2 = insert_task(jobs, "jobs", payload="b")

    rows = acquired(jobs, 10)
    assert {r[0] for r in rows} == {id1, id2}
    assert all(r[1] == 1 for r in rows)


def test_acquire_increments_execution_count_on_retry(jobs):
    task_id = insert_task(jobs, "jobs", payload="a")
    acquired(jobs)
    with jobs.cursor() as cur:
        cur.execute("SELECT tasks.retry_tasks_jobs(%s, now())", (task_id,))
    acquired(jobs)

    with jobs.cursor() as cur:
        cur.execute("SELECT execution_count FROM tasks.jobs WHERE id = %s", (task_id,))
        assert cur.fetchone()[0] == 2


def test_acquire_respects_limit_and_planned_to(jobs):
    id_now = insert_task(jobs, "jobs", payload="now")
    with jobs.cursor() as cur:
        cur.execute(
            "INSERT INTO tasks.jobs(planned_to, payload) "
            "VALUES (now() + interval '1 hour', 'future') RETURNING id"
        )
        id_future = cur.fetchone()[0]

        rows = acquired(jobs, 1)
        assert len(rows) == 1
        assert rows[0][0] == id_now
        assert rows[0][0] != id_future


def test_acquire_skips_locked_rows(committed_db):
    create_task_table(committed_db, "jobs")
    register_type(committed_db, "jobs", timeout="00:05:00", max_retries=3)
    id1 = insert_task(committed_db, "jobs", payload="a")
    insert_task(committed_db, "jobs", payload="b")

    holder = psycopg.connect(DSN)
    holder.autocommit = False
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT id FROM tasks.jobs WHERE id = %s FOR UPDATE", (id1,))
            rows = acquired(committed_db, 10)
            assert id1 not in {r[0] for r in rows}
    finally:
        holder.rollback()
        holder.close()


def test_finish_marks_completed(jobs):
    task_id = insert_task(jobs, "jobs", payload="a")
    acquired(jobs)

    with jobs.cursor() as cur:
        cur.execute("SELECT started_at FROM tasks.jobs WHERE id = %s", (task_id,))
        started = cur.fetchone()[0]
        cur.execute("SELECT tasks.finish_tasks_jobs(%s, %s, 'done')", (task_id, started))
        cur.execute(
            "SELECT finished_at IS NOT NULL, comment FROM tasks.jobs WHERE id = %s",
            (task_id,),
        )
        finished, comment = cur.fetchone()
    assert finished is True
    assert comment == "done"


def test_finish_only_unfinished(jobs):
    task_id = insert_task(jobs, "jobs", payload="a")
    acquired(jobs)
    with jobs.cursor() as cur:
        cur.execute("SELECT started_at FROM tasks.jobs WHERE id = %s", (task_id,))
        started = cur.fetchone()[0]
        cur.execute("SELECT tasks.finish_tasks_jobs(%s, %s, 'first')", (task_id, started))
        # Second finish must be a no-op.
        cur.execute("SELECT tasks.finish_tasks_jobs(%s, %s, 'second')", (task_id, started))
        cur.execute("SELECT comment FROM tasks.jobs WHERE id = %s", (task_id,))
        assert cur.fetchone()[0] == "first"


def test_retry_rearms_task(jobs):
    task_id = insert_task(jobs, "jobs", payload="a")
    acquired(jobs)

    with jobs.cursor() as cur:
        cur.execute(
            "SELECT tasks.retry_tasks_jobs(%s, now() + interval '1 sec')", (task_id,)
        )
        cur.execute(
            "SELECT started_at IS NULL, planned_to > now() FROM tasks.jobs WHERE id = %s",
            (task_id,),
        )
        assert cur.fetchone() == (True, True)


@pytest.mark.xfail(reason="retry with default NULL planned_to leaves planned_to NULL",
                   strict=False)
def test_retry_default_planned_to_rearms(jobs):
    task_id = insert_task(jobs, "jobs", payload="a")
    acquired(jobs)

    with jobs.cursor() as cur:
        cur.execute("SELECT tasks.retry_tasks_jobs(%s)", (task_id,))
        cur.execute(
            "SELECT planned_to IS NOT NULL, started_at IS NULL FROM tasks.jobs WHERE id = %s",
            (task_id,),
        )
        assert cur.fetchone() == (True, True)


def test_cancel_stops_acquire(jobs):
    task_id = insert_task(jobs, "jobs", payload="a")

    with jobs.cursor() as cur:
        cur.execute("SELECT tasks.cancel_tasks_jobs(%s)", (task_id,))
    assert acquired(jobs) == []

    with jobs.cursor() as cur:
        cur.execute("SELECT canceled FROM tasks.jobs WHERE id = %s", (task_id,))
        assert cur.fetchone()[0] is True


def test_reset_clears_state(jobs):
    task_id = insert_task(jobs, "jobs", payload="a")
    acquired(jobs)
    with jobs.cursor() as cur:
        cur.execute("SELECT started_at FROM tasks.jobs WHERE id = %s", (task_id,))
        started = cur.fetchone()[0]
        cur.execute("SELECT tasks.finish_tasks_jobs(%s, %s)", (task_id, started))
        cur.execute("SELECT tasks.reset_tasks_jobs(%s)", (task_id,))
        cur.execute(
            "SELECT execution_count, finished_at, canceled FROM tasks.jobs WHERE id = %s",
            (task_id,),
        )
        assert cur.fetchone() == (0, None, False)


def test_clean_removes_finished_after_storage_time(jobs):
    task_id = insert_task(jobs, "jobs", payload="old")
    acquired(jobs)
    with jobs.cursor() as cur:
        cur.execute("SELECT started_at FROM tasks.jobs WHERE id = %s", (task_id,))
        started = cur.fetchone()[0]
        cur.execute("SELECT tasks.finish_tasks_jobs(%s, %s)", (task_id, started))
        cur.execute(
            "UPDATE tasks.jobs SET finished_at = now() - interval '2 days' WHERE id = %s",
            (task_id,),
        )
        cur.execute("SELECT tasks.clean_tasks_jobs('1 day')")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM tasks.jobs WHERE id = %s", (task_id,))
        assert cur.fetchone()[0] == 0


@pytest.mark.xfail(reason="max_retries acquire condition appears off by one",
                   strict=False)
def test_max_retries_bounds_acquires(jobs):
    """max_retries should cap the number of times a task can be acquired."""
    task_id = insert_task(jobs, "jobs", payload="a")

    count = 0
    with jobs.cursor() as cur:
        while count < 100:
            cur.execute("SELECT id FROM tasks.acquire_tasks_jobs(1)")
            if not cur.fetchall():
                break
            count += 1
            cur.execute("SELECT tasks.retry_tasks_jobs(%s, now())", (task_id,))

    assert count == 3