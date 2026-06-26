DROP FUNCTION tasks.unregister;

CREATE FUNCTION tasks.unregister(
    schema_name text,
    table_name text
) RETURNS VOID
LANGUAGE plpgsql AS $$
DECLARE
    removed_entries int;
BEGIN
    -- Remove entry from tasks registry
    WITH deleted AS (
        DELETE FROM tasks.types WHERE
            types.schema_name = unregister.schema_name AND
            types.table_name = unregister.table_name
        RETURNING id
    )
    SELECT count(*) INTO STRICT removed_entries FROM deleted;

    IF removed_entries = 0 THEN
        RETURN;
    END IF;

    -- Remove acquire func for task
    EXECUTE format(
        'DROP FUNCTION tasks.acquire_%s_%s',
        schema_name, table_name
    );

    -- Remove finish func for task
    EXECUTE format(
        'DROP FUNCTION tasks.finish_%s_%s',
        schema_name, table_name
    );

    -- Remove retry func for task
    EXECUTE format(
        'DROP FUNCTION tasks.retry_%s_%s',
        schema_name, table_name
    );

    -- Remove cancel func for task
    EXECUTE format(
        'DROP FUNCTION tasks.cancel_%s_%s',
        schema_name, table_name
    );

    -- Remove reset func for task
    EXECUTE format(
        'DROP FUNCTION tasks.reset_%s_%s',
        schema_name, table_name
    );

    -- Remove clean func for task
    EXECUTE format(
        'DROP FUNCTION tasks.clean_%s_%s',
       schema_name, table_name
    );

    -- Remove stats view for task
    DROP VIEW IF EXISTS tasks.stats;

    -- Remove notifications trigger
    EXECUTE format(
        'DROP TRIGGER IF EXISTS notify_on_new ON %s.%s',
       schema_name, table_name
    );

    -- Remove stats view for task
    EXECUTE format(
        'DROP VIEW tasks.stats_of_%s_%s',
        schema_name, table_name
    );

    -- Recreate generic stats view
    PERFORM tasks.create_stats_view();
END
$$;
