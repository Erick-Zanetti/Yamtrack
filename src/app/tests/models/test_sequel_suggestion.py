"""Tests for the SequelSuggestion model."""
from django.contrib.auth import get_user_model
from django.db import IntegrityError
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


class SequelSuggestionModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="p")
        self.root_item = Item.objects.create(
            media_id="1",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Root",
            image="u",
        )
        self.target_item = Item.objects.create(
            media_id="2",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Sequel",
            image="u",
        )
        self.root = Anime.objects.create(
            item=self.root_item,
            user=self.user,
            status=Status.PLANNING.value,
        )
        Anime.objects.filter(pk=self.root.pk).update(
            status=Status.COMPLETED.value,
        )

    def test_defaults(self):
        s = SequelSuggestion.objects.create(
            user=self.user,
            root=self.root,
            target=self.target_item,
        )
        self.assertEqual(s.additional_count, 0)
        self.assertEqual(s.chain_depth, 1)
        self.assertEqual(s.target_release_status, "")
        self.assertIsNone(s.dismissed_at)
        self.assertIsNotNone(s.created_at)
        self.assertIsNotNone(s.refreshed_at)

    def test_unique_per_user_root_target(self):
        SequelSuggestion.objects.create(
            user=self.user,
            root=self.root,
            target=self.target_item,
        )
        with self.assertRaises(IntegrityError):
            SequelSuggestion.objects.create(
                user=self.user,
                root=self.root,
                target=self.target_item,
            )

    def test_multiple_targets_for_same_root_allowed(self):
        other_target = Item.objects.create(
            media_id="3",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Other",
            image="u",
        )
        SequelSuggestion.objects.create(
            user=self.user,
            root=self.root,
            target=self.target_item,
        )
        SequelSuggestion.objects.create(
            user=self.user,
            root=self.root,
            target=other_target,
        )
        self.assertEqual(
            SequelSuggestion.objects.filter(user=self.user, root=self.root).count(),
            2,
        )

    def test_cascade_on_user_delete(self):
        SequelSuggestion.objects.create(
            user=self.user,
            root=self.root,
            target=self.target_item,
        )
        self.user.delete()
        self.assertEqual(SequelSuggestion.objects.count(), 0)

    def test_cascade_on_root_delete(self):
        SequelSuggestion.objects.create(
            user=self.user,
            root=self.root,
            target=self.target_item,
        )
        self.root.delete()
        self.assertEqual(SequelSuggestion.objects.count(), 0)

    def test_cascade_on_target_delete(self):
        SequelSuggestion.objects.create(
            user=self.user,
            root=self.root,
            target=self.target_item,
        )
        self.target_item.delete()
        self.assertEqual(SequelSuggestion.objects.count(), 0)

    def test_ordering_by_refreshed_desc(self):
        other_target = Item.objects.create(
            media_id="3",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Other",
            image="u",
        )
        older = SequelSuggestion.objects.create(
            user=self.user,
            root=self.root,
            target=self.target_item,
        )
        newer = SequelSuggestion.objects.create(
            user=self.user,
            root=self.root,
            target=other_target,
        )
        ordered = list(SequelSuggestion.objects.all())
        self.assertEqual(ordered[0].pk, newer.pk)
        self.assertEqual(ordered[1].pk, older.pk)
