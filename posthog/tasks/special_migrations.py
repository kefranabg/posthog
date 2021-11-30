from logging import error

from celery.result import AsyncResult

from posthog.celery import app
from posthog.models.special_migration import MigrationStatus, SpecialMigration, get_all_running_special_migrations
from posthog.special_migrations.runner import (
    force_stop_migration,
    process_error,
    run_migration_healthcheck,
    run_special_migration_next_op,
    start_special_migration,
    update_migration_progress,
)


class CeleryTaskState:
    Pending = "PENDING"
    Started = "STARTED"
    Retry = "RETRY"
    Failure = "FAILURE"
    Success = "SUCCESS"


# we're hijacking an entire worker to do this - consider:
# 1. spawning a thread within the worker
# 2. suggesting users scale celery when running special migrations
# 3. ...
@app.task(ignore_result=False, max_retries=0)
def run_special_migration(migration_name: str, start=True) -> None:
    if start:
        start_special_migration(migration_name)
        return

    # TODO: Implement resumable operations
    run_special_migration_next_op(migration_name)


@app.task(ignore_result=False, track_started=True, max_retries=0)
def check_special_migration_health() -> None:
    migration_instance = SpecialMigration.objects.get(status=MigrationStatus.Running)
    if not migration_instance:
        return

    migration_task_celery_state = AsyncResult(migration_instance.celery_task_id).state

    # we only care about "supposedly running" tasks
    # failures and successes are handled elsewhere
    # pending means we haven't picked up the task yet
    # retry is not possible as max_retries == 0
    if migration_task_celery_state != CeleryTaskState.Started:
        return

    inspector = app.control.inspect()
    active_tasks_per_node = inspector.active()

    active_task_ids = []

    for _, tasks in active_tasks_per_node:
        active_task_ids += [task["id"] for task in tasks]

    # the worker crashed - this is how we find out and process the error
    if migration_instance.celery_task_id not in active_task_ids:
        process_error(migration_instance, "Celery worker crashed while running migration.")
        return

    ok, error = run_migration_healthcheck(migration_instance)

    if not ok:
        force_stop_migration(migration_instance, f"Healthcheck failed with error: {error}")
        return

    update_migration_progress(migration_instance)
