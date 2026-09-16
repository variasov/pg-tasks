-- Fix register():
-- 1. Persist notification_interval into tasks.types.
-- 2. Format max_retries as a literal so a NULL default does not break acquire SQL.
-- 3. Default retry(planned_to) to now() when omitted.
-- 4. Treat max_retries as the inclusive number of attempts (no off-by-one).
CREATE OR REPLACE FUNCTION tasks.register(
    schema_name text,
    table_name text,
    timeout interval DEFAULT NULL,
    max_retries int DEFAULT NULL,
    notification_interval interval DEFAULT '1s',
    storage_time interval DEFAULT NULL
) RETURNS VOID
LANGUAGE plpgsql AS $$
BEGIN

    -- Remove previous version of task objects, if exists
    PERFORM tasks.unregister(schema_name, table_name);

    -- Add entry with task parameters to tasks registry
    INSERT INTO tasks.types(
        schema_name, table_name, timeout,
        notification_interval, max_retries, storage_time
    ) VALUES (
        schema_name, table_name, timeout,
        notification_interval, max_retries, storage_time
    );

    -- Create trigger for notifications
    IF notification_interval IS NOT NULL THEN
        EXECUTE format(
            $QUERY$
                CREATE TRIGGER notify_on_new
                AFTER INSERT ON %1$I.%2$I
                FOR EACH STATEMENT
                EXECUTE FUNCTION tasks.notify_workers_throttled(%3$L);
            $QUERY$,
            schema_name,
            table_name,
            notification_interval
        );
    ELSE
        EXECUTE format(
            $QUERY$
                CREATE TRIGGER notify_on_new
                AFTER INSERT ON %1$I.%2$I
                FOR EACH STATEMENT
                EXECUTE FUNCTION tasks.notify_workers();
            $QUERY$,
            schema_name,
            table_name
        );
    END IF;

    -- Create acquire func for task
    EXECUTE format(
        $QUERY$
            CREATE OR REPLACE FUNCTION
            %1$I.%2$s(max_num int)
            RETURNS SETOF %3$s.%4$s
            LANGUAGE sql VOLATILE AS $FUNC$
                UPDATE %3$I.%4$I
                SET
                    execution_count = execution_count + 1,
                    started_at = now()
                FROM (
                    SELECT id
                    FROM %3$I.%4$I
                    WHERE
                        finished_at IS NULL
                        AND canceled IS NOT TRUE
                        AND (
                            %5$L IS NULL OR
                            execution_count < %5$L
                        )
                        AND planned_to <= now()
                        AND (
                            started_at IS NULL OR
                            now() - started_at > %6$L
                        )
                    ORDER BY created_at DESC
                    LIMIT max_num
                    FOR UPDATE SKIP LOCKED
                ) as acquired_tasks
                WHERE %3$I.%4$I.id = acquired_tasks.id
                RETURNING %3$I.%4$I.*
            $FUNC$;
        $QUERY$,
        'tasks',
        format('acquire_%s_%s', schema_name, table_name),
        schema_name,
        table_name,
        max_retries,
        timeout
    );

    -- Create finish func for task
    EXECUTE format(
        $QUERY$
            CREATE OR REPLACE FUNCTION
            %1$I.%2$s(
                task_id int,
                started_at timestamptz,
                comment text DEFAULT NULL
            )
            RETURNS VOID
            LANGUAGE sql VOLATILE AS $FUNC$
                UPDATE %3$I.%4$I
                SET
                    finished_at = now(),
                    comment = %2$s.comment,
                    started_at = %2$s.started_at
                WHERE
                    finished_at IS NULL AND
                    canceled IS NOT TRUE AND
                    id = task_id;
            $FUNC$;
        $QUERY$,
        'tasks',
        format('finish_%s_%s', schema_name, table_name),
        schema_name,
        table_name
    );

    -- Create retry func for task
    EXECUTE format(
        $QUERY$
            CREATE OR REPLACE FUNCTION
            %1$s.%2$s(
                task_id int,
                planned_to timestamptz DEFAULT NULL,
                comment text DEFAULT NULL
            ) RETURNS VOID
            LANGUAGE sql VOLATILE
            AS $FUNC$
                UPDATE %3$I.%4$I
                SET
                    started_at = NULL,
                    planned_to = COALESCE(%2$s.planned_to, now())
                WHERE
                    finished_at IS NULL AND
                    canceled IS NOT TRUE AND
                    id = task_id;
            $FUNC$;
        $QUERY$,
        'tasks',
        format('retry_%s_%s', schema_name, table_name),
        schema_name,
        table_name
    );

    -- Create cancel func for task
    EXECUTE format(
        $QUERY$
            CREATE OR REPLACE FUNCTION
            %1$s.%2$s(
                task_id int,
                planned_to timestamptz DEFAULT NULL,
                comment text DEFAULT NULL
            ) RETURNS VOID
            LANGUAGE sql VOLATILE
            AS $FUNC$
                UPDATE %3$I.%4$I
                SET
                    canceled = TRUE
                WHERE
                    finished_at IS NULL AND
                    id = task_id;
            $FUNC$;
        $QUERY$,
        'tasks',
        format('cancel_%s_%s', schema_name, table_name),
        schema_name,
        table_name
    );

    -- Create reset func for task
    EXECUTE format(
        $QUERY$
            CREATE OR REPLACE FUNCTION %1$s.%2$s(task_id int)
            RETURNS VOID
            LANGUAGE sql VOLATILE AS $FUNC$
                UPDATE %3$I.%4$I
                SET
                    started_at = NULL,
                    finished_at = NULL,
                    execution_count = 0,
                    canceled = False
                WHERE id = task_id;
                NOTIFY pg_tasks, '%3$I.%4$I';
            $FUNC$;
        $QUERY$,
        'tasks',
        format('reset_%s_%s', schema_name, table_name),
        schema_name,
        table_name
    );

    -- Create clean func for task
    EXECUTE format(
        $QUERY$
            CREATE OR REPLACE FUNCTION %1$s.%2$s(
                finished_after interval DEFAULT %5$L
            ) RETURNS int
            LANGUAGE sql VOLATILE AS $FUNC$
                WITH deleted AS (
                    DELETE FROM %3$I.%4$I
                    WHERE now() - finished_at > finished_after
                    RETURNING id
                )
                    SELECT count(*) FROM deleted;
            $FUNC$;
        $QUERY$,
        'tasks',
        format('clean_%s_%s', schema_name, table_name),
        schema_name,
        table_name,
        storage_time
    );

    -- Create stats view for task
    EXECUTE format(
        $QUERY$
            CREATE VIEW %1$I.%2$I AS
            SELECT
                count(*) AS total,
                count(*) FILTER (WHERE planned_to_future) AS planned_to_future,
                count(*) FILTER (WHERE completed) AS completed,
                count(*) FILTER (WHERE completed AND retried) AS completed_with_retry,
                count(*) FILTER (WHERE completed_with_late) AS completed_with_late,
                count(*) FILTER (WHERE canceled) AS canceled,
                count(*) FILTER (WHERE not_started) AS not_started,
                count(*) FILTER (WHERE timeout) AS timeout
            FROM (
                SELECT
                    id,
                    planned_to > now() AS planned_to_future,
                    finished_at IS NOT NULL AS completed,
                    canceled IS TRUE as canceled,
                    execution_count > 1 AS retried,
                    (
                        started_at IS NULL AND
                        planned_to > now()
                    ) AS not_started,
                    (
                        started_at IS NOT NULL AND
                        finished_at IS NULL AND
                        now() - started_at > %5$L
                    ) AS timeout,
                    (
                        %5$L IS NOT NULL AND
                        started_at IS NOT NULL AND
                        finished_at IS NOT NULL AND
                        finished_at - started_at > %5$L
                    ) AS completed_with_late
                FROM %3$I.%4$I
            ) AS tasks;
        $QUERY$,
        'tasks',
        format('stats_of_%s_%s', schema_name, table_name),
        schema_name,
        table_name,
        timeout
    );

    -- Remove stats view for task
    DROP VIEW IF EXISTS tasks.stats;

    PERFORM tasks.create_stats_view();
END
$$;