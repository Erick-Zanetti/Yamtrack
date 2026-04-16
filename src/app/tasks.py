import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from app.models import Anime, SequelSuggestion, Status, UserMessage
from app.sequels import walk_sequel_chain

logger = logging.getLogger(__name__)


@shared_task(name="Cleanup user messages")
def cleanup_user_messages():
    """Delete shown user messages older than the configured retention window."""
    cutoff = timezone.now() - timedelta(days=settings.USER_MESSAGE_RETENTION_DAYS)
    deleted_count, _ = UserMessage.objects.filter(
        shown_at__isnull=False,
        shown_at__lt=cutoff,
    ).delete()

    logger.info("Deleted %s old shown user messages.", deleted_count)

    return deleted_count


@shared_task(name="Refresh sequel suggestions for user")
def refresh_sequel_suggestions_for_user(user_id):
    """Rebuild SequelSuggestion rows for a single user from their Completed anime."""
    user_model = get_user_model()
    try:
        user = user_model.objects.get(pk=user_id)
    except user_model.DoesNotExist:
        logger.warning("refresh_sequel_suggestions_for_user: user %s not found", user_id)
        return

    kept_pks = set()
    completed = (
        Anime.objects
        .filter(user=user, status=Status.COMPLETED.value)
        .select_related("item")
    )

    for anime in completed:
        try:
            result = walk_sequel_chain(user, anime)
        except Exception:
            logger.exception(
                "sequel chain walk failed: user=%s anime=%s",
                user.pk,
                anime.pk,
            )
            continue
        if result is None:
            continue
        target, additional, depth, release_status = result
        suggestion, _ = SequelSuggestion.objects.update_or_create(
            user=user,
            root=anime,
            target=target,
            defaults={
                "additional_count": additional,
                "chain_depth": depth,
                "target_release_status": release_status,
            },
        )
        kept_pks.add(suggestion.pk)

    SequelSuggestion.objects.filter(user=user).exclude(pk__in=kept_pks).delete()


@shared_task(name="Refresh all sequel suggestions")
def refresh_all_sequel_suggestions():
    """Fan out per-user sequel refresh tasks. Scheduled via celery-beat."""
    user_model = get_user_model()
    for user_id in user_model.objects.values_list("pk", flat=True):
        refresh_sequel_suggestions_for_user.delay(user_id)
