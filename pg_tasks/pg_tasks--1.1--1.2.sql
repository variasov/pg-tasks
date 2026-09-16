-- Recreate the last_notifications.task_type foreign key with ON DELETE CASCADE
-- so that unregister() removes any live throttling row together with the
-- registry entry instead of failing and leaving a half-removed task type.
ALTER TABLE tasks.last_notifications
    DROP CONSTRAINT last_notifications_task_type_fkey;

ALTER TABLE tasks.last_notifications
    ADD CONSTRAINT last_notifications_task_type_fkey
    FOREIGN KEY (task_type) REFERENCES tasks.types(id) ON DELETE CASCADE;