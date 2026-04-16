import logging

from celery import states
from celery.signals import before_task_publish
from django.db.backends.signals import connection_created
from django.db.models.signals import post_save
from django.dispatch import receiver
from django_celery_results.models import TaskResult

from app.models import Anime, SequelSuggestion, Status

logger = logging.getLogger(__name__)

_HIDE_TARGET_STATUSES = frozenset({
    Status.IN_PROGRESS.value,
    Status.COMPLETED.value,
    Status.DROPPED.value,
})


@receiver(connection_created)
def setup_sqlite_pragmas(sender, connection, **kwargs):  # noqa: ARG001
    """Set up SQLite pragmas for WAL mode and busy timeout on connection creation."""
    if connection.vendor == "sqlite":
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=wal;")
        cursor.execute("PRAGMA busy_timeout=5000;")
        cursor.close()


@receiver(post_save, sender=Anime)
def invalidate_sequel_suggestions_on_status_change(sender, instance, **kwargs):  # noqa: ARG001
    """Invalidate SequelSuggestion rows when an Anime's status transitions."""
    tracker = instance.tracker
    if "status" not in tracker.changed():
        return

    if instance.status in _HIDE_TARGET_STATUSES:
        SequelSuggestion.objects.filter(
            user=instance.user,
            target=instance.item,
        ).delete()

    previous = tracker.previous("status")
    if previous == Status.COMPLETED.value and instance.status != Status.COMPLETED.value:
        SequelSuggestion.objects.filter(root=instance).delete()


@before_task_publish.connect
def create_task_result_on_publish(sender=None, headers=None, body=None, **kwargs):  # noqa: ARG001
    """Create a TaskResult object with PENDING status on task publish.

    https://github.com/celery/django-celery-results/issues/286#issuecomment-1279161047
    """
    if "task" not in headers:
        return

    TaskResult.objects.store_result(
        content_type="application/json",
        content_encoding="utf-8",
        task_id=headers["id"],
        result=None,
        status=states.PENDING,
        task_name=headers["task"],
        task_args=headers.get("argsrepr", ""),
        task_kwargs=headers.get("kwargsrepr", ""),
    )
