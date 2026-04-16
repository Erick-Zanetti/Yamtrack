"""Tests for sequel suggestion invalidation signals."""
from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import (
    Anime,
    Item,
    MediaTypes,
    SequelSuggestion,
    Sources,
    Status,
)

User = get_user_model()


def _item(media_id, title="x"):
    return Item.objects.create(
        media_id=str(media_id),
        source=Sources.MAL.value,
        media_type=MediaTypes.ANIME.value,
        title=title,
        image="u",
    )


def _anime(user, item, status=Status.PLANNING.value):
    """Create an Anime, flipping to the target status via update() to skip save()."""
    a = Anime.objects.create(item=item, user=user, status=Status.PLANNING.value)
    if status != Status.PLANNING.value:
        Anime.objects.filter(pk=a.pk).update(status=status)
        a.refresh_from_db()
    return a


class InvalidateOnTargetBecomingHiddenTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="p")
        self.root_item = _item(1, "root")
        self.target_item = _item(2, "target")
        self.root = _anime(self.user, self.root_item, Status.COMPLETED.value)

    def _seed_suggestion(self):
        return SequelSuggestion.objects.create(
            user=self.user,
            root=self.root,
            target=self.target_item,
        )

    def test_target_becomes_in_progress_deletes_suggestion(self):
        self._seed_suggestion()
        target_anime = _anime(self.user, self.target_item, Status.PLANNING.value)
        # Now flip to IN_PROGRESS via save() to trigger the signal.
        target_anime.status = Status.IN_PROGRESS.value
        target_anime.save()
        self.assertFalse(
            SequelSuggestion.objects.filter(target=self.target_item).exists(),
        )

    def test_target_becomes_completed_deletes_suggestion(self):
        self._seed_suggestion()
        target_anime = _anime(self.user, self.target_item, Status.PLANNING.value)
        target_anime.status = Status.COMPLETED.value
        # Avoid calling the expensive process_status — patch to skip the API call.
        from unittest.mock import patch
        with patch.object(type(target_anime), "process_status"):
            target_anime.save()
        self.assertFalse(
            SequelSuggestion.objects.filter(target=self.target_item).exists(),
        )

    def test_target_becomes_dropped_deletes_suggestion(self):
        self._seed_suggestion()
        target_anime = _anime(self.user, self.target_item, Status.PLANNING.value)
        target_anime.status = Status.DROPPED.value
        target_anime.save()
        self.assertFalse(
            SequelSuggestion.objects.filter(target=self.target_item).exists(),
        )

    def test_target_becomes_planning_keeps_suggestion(self):
        """Planning is a visible status (decision #7) — do NOT delete."""
        self._seed_suggestion()
        other_item = _item(3, "other")
        # Create new Anime for target as Paused first, then flip to Planning.
        a = Anime.objects.create(
            item=self.target_item,
            user=self.user,
            status=Status.PAUSED.value,
        )
        a.status = Status.PLANNING.value
        a.save()
        self.assertTrue(
            SequelSuggestion.objects.filter(target=self.target_item).exists(),
        )


class InvalidateOnRootLeavingCompletedTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="p")
        self.root_item = _item(1, "root")
        self.target_item = _item(2, "target")
        self.root = _anime(self.user, self.root_item, Status.COMPLETED.value)
        SequelSuggestion.objects.create(
            user=self.user,
            root=self.root,
            target=self.target_item,
        )

    def test_root_completed_to_in_progress_deletes(self):
        self.root.status = Status.IN_PROGRESS.value
        self.root.save()
        self.assertFalse(
            SequelSuggestion.objects.filter(root=self.root).exists(),
        )

    def test_root_completed_to_dropped_deletes(self):
        self.root.status = Status.DROPPED.value
        self.root.save()
        self.assertFalse(
            SequelSuggestion.objects.filter(root=self.root).exists(),
        )


class UnchangedStatusDoesNotDeleteTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="p")
        self.root_item = _item(1, "root")
        self.target_item = _item(2, "target")
        self.root = _anime(self.user, self.root_item, Status.COMPLETED.value)
        self.suggestion = SequelSuggestion.objects.create(
            user=self.user,
            root=self.root,
            target=self.target_item,
        )

    def test_save_without_status_change_preserves_suggestion(self):
        # Modify a non-status field
        self.root.notes = "something new"
        self.root.save()
        self.assertTrue(
            SequelSuggestion.objects.filter(pk=self.suggestion.pk).exists(),
        )


class CascadeOnAnimeDeletionTests(TestCase):
    """Deleting the root Anime cascades — no signal needed; verifies cascade works."""

    def setUp(self):
        self.user = User.objects.create_user(username="u", password="p")
        self.root_item = _item(1, "root")
        self.target_item = _item(2, "target")
        self.root = _anime(self.user, self.root_item, Status.COMPLETED.value)
        SequelSuggestion.objects.create(
            user=self.user,
            root=self.root,
            target=self.target_item,
        )

    def test_deleting_root_anime_cascades_suggestions(self):
        self.root.delete()
        self.assertFalse(
            SequelSuggestion.objects.filter(target=self.target_item).exists(),
        )
