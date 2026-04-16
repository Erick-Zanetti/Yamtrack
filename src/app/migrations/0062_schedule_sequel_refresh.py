"""Schedule the daily sequel-suggestions refresh periodic task."""
from django.db import migrations


TASK_NAME = "Refresh all sequel suggestions"


def create_periodic_task(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    crontab, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="4",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="UTC",
    )
    PeriodicTask.objects.get_or_create(
        name=TASK_NAME,
        defaults={
            "task": TASK_NAME,
            "crontab": crontab,
            "enabled": True,
        },
    )


def delete_periodic_task(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0061_sequelsuggestion"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(create_periodic_task, reverse_code=delete_periodic_task),
    ]
