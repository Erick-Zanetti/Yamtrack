"""Tests for sequel suggestion Celery tasks."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from django_celery_beat.models import PeriodicTask

from app.models import (
    Anime,
    Item,
    MediaTypes,
    SequelSuggestion,
    Sources,
    Status,
)
from app.tasks import (
    refresh_all_sequel_suggestions,
    refresh_sequel_suggestions_for_user,
)

User = get_user_model()


def _completed_anime(user, media_id, title="Anime"):
    item = Item.objects.create(
        media_id=str(media_id),
        source=Sources.MAL.value,
        media_type=MediaTypes.ANIME.value,
        title=title,
        image="u",
    )
    anime = Anime.objects.create(item=item, user=user, status=Status.PLANNING.value)
    Anime.objects.filter(pk=anime.pk).update(status=Status.COMPLETED.value)
    anime.refresh_from_db()
    return anime


def _target_item(media_id, title="Target"):
    return Item.objects.create(
        media_id=str(media_id),
        source=Sources.MAL.value,
        media_type=MediaTypes.ANIME.value,
        title=title,
        image="u",
    )


class RefreshSequelSuggestionsForUserTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="p")
        self.other_user = User.objects.create_user(username="o", password="p")

    def test_creates_rows_for_completed_with_chain_result(self):
        """One SequelSuggestion per completed anime that produces a chain result."""
        a1 = _completed_anime(self.user, 1)
        a2 = _completed_anime(self.user, 2)
        a3 = _completed_anime(self.user, 3)
        # other user has completed anime — should not affect user_a
        _completed_anime(self.other_user, 4)

        t_for_1 = _target_item(101)
        t_for_3 = _target_item(103)

        def walk_side_effect(user, anime):
            mapping = {
                a1.pk: (t_for_1, 0, 1, "Finished"),
                a2.pk: None,
                a3.pk: (t_for_3, 2, 1, "Airing"),
            }
            return mapping.get(anime.pk)

        with patch(
            "app.tasks.walk_sequel_chain",
            side_effect=walk_side_effect,
        ) as mock_walk:
            refresh_sequel_suggestions_for_user(self.user.pk)

        self.assertEqual(mock_walk.call_count, 3)  # user_a's 3 completed
        self.assertEqual(
            SequelSuggestion.objects.filter(user=self.user).count(),
            2,
        )
        self.assertEqual(
            SequelSuggestion.objects.filter(user=self.other_user).count(),
            0,
        )
        s1 = SequelSuggestion.objects.get(user=self.user, root=a1)
        self.assertEqual(s1.target_id, t_for_1.pk)
        self.assertEqual(s1.additional_count, 0)
        self.assertEqual(s1.target_release_status, "Finished")
        s3 = SequelSuggestion.objects.get(user=self.user, root=a3)
        self.assertEqual(s3.additional_count, 2)
        self.assertEqual(s3.target_release_status, "Airing")

    def test_rerun_updates_in_place(self):
        """Second run with changed fields uses update_or_create (one row)."""
        a1 = _completed_anime(self.user, 1)
        target = _target_item(101)

        with patch(
            "app.tasks.walk_sequel_chain",
            return_value=(target, 0, 1, "Finished"),
        ):
            refresh_sequel_suggestions_for_user(self.user.pk)

        self.assertEqual(SequelSuggestion.objects.count(), 1)

        with patch(
            "app.tasks.walk_sequel_chain",
            return_value=(target, 2, 3, "Airing"),
        ):
            refresh_sequel_suggestions_for_user(self.user.pk)

        self.assertEqual(SequelSuggestion.objects.count(), 1)
        s = SequelSuggestion.objects.get(user=self.user, root=a1)
        self.assertEqual(s.additional_count, 2)
        self.assertEqual(s.chain_depth, 3)
        self.assertEqual(s.target_release_status, "Airing")

    def test_gc_deletes_stale_rows(self):
        """Rows not re-emitted this run are deleted."""
        a1 = _completed_anime(self.user, 1)
        stale_target = _target_item(999)
        # Seed a row that will NOT be re-emitted (chain returns None).
        SequelSuggestion.objects.create(
            user=self.user,
            root=a1,
            target=stale_target,
        )

        with patch("app.tasks.walk_sequel_chain", return_value=None):
            refresh_sequel_suggestions_for_user(self.user.pk)

        self.assertEqual(SequelSuggestion.objects.count(), 0)

    def test_dismissed_at_preserved_on_rerun(self):
        """dismissed_at survives re-runs — defaults dict must not include it."""
        a1 = _completed_anime(self.user, 1)
        target = _target_item(101)
        SequelSuggestion.objects.create(
            user=self.user,
            root=a1,
            target=target,
            dismissed_at=timezone.now(),
        )

        with patch(
            "app.tasks.walk_sequel_chain",
            return_value=(target, 5, 2, "Airing"),
        ):
            refresh_sequel_suggestions_for_user(self.user.pk)

        s = SequelSuggestion.objects.get(user=self.user, root=a1, target=target)
        self.assertIsNotNone(s.dismissed_at)
        self.assertEqual(s.additional_count, 5)

    def test_chain_walk_exception_logged_and_skipped(self):
        """If walk raises on one anime, the task continues with others."""
        a1 = _completed_anime(self.user, 1)
        a2 = _completed_anime(self.user, 2)
        target = _target_item(102)

        def side_effect(user, anime):
            if anime.pk == a1.pk:
                raise RuntimeError("boom")
            return (target, 0, 1, "Finished")

        with patch("app.tasks.walk_sequel_chain", side_effect=side_effect):
            refresh_sequel_suggestions_for_user(self.user.pk)

        self.assertEqual(
            SequelSuggestion.objects.filter(user=self.user).count(),
            1,
        )
        self.assertTrue(
            SequelSuggestion.objects.filter(user=self.user, root=a2).exists(),
        )


class RefreshAllSequelSuggestionsTests(TestCase):
    def test_fans_out_per_user(self):
        u1 = User.objects.create_user(username="u1", password="p")
        u2 = User.objects.create_user(username="u2", password="p")

        with patch(
            "app.tasks.refresh_sequel_suggestions_for_user.delay",
        ) as mock_delay:
            refresh_all_sequel_suggestions()

        called_user_ids = {c.args[0] for c in mock_delay.call_args_list}
        self.assertIn(u1.pk, called_user_ids)
        self.assertIn(u2.pk, called_user_ids)


class SequelRefreshPeriodicTaskMigrationTests(TestCase):
    """The data migration should install a daily 04:00 UTC PeriodicTask."""

    def test_periodic_task_row_exists(self):
        qs = PeriodicTask.objects.filter(task="Refresh all sequel suggestions")
        self.assertTrue(qs.exists())
        pt = qs.first()
        self.assertTrue(pt.enabled)
        self.assertEqual(pt.crontab.hour, "4")
        self.assertEqual(pt.crontab.minute, "0")
