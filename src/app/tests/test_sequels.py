"""Tests for app.sequels.walk_sequel_chain."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase

from app.models import Anime, Item, MediaTypes, Sources, Status
from app.sequels import MAX_DEPTH, walk_sequel_chain

User = get_user_model()


def _meta(media_id, *, related_anime=None, release_status="Finished"):
    """Build a minimal mal.anime() return shape."""
    return {
        "media_id": str(media_id),
        "source": Sources.MAL.value,
        "media_type": MediaTypes.ANIME.value,
        "title": f"A{media_id}",
        "image": f"img/{media_id}",
        "details": {"status": release_status},
        "related": {
            "related_anime": related_anime or [],
            "recommendations": [],
        },
    }


def _sequel(media_id, *, title=None, image=None, relation_type="sequel"):
    return {
        "media_id": media_id,
        "source": Sources.MAL.value,
        "media_type": MediaTypes.ANIME.value,
        "title": title or f"A{media_id}",
        "image": image or f"img/{media_id}",
        "relation_type": relation_type,
    }


class WalkSequelChainTests(TestCase):
    """Pure logic tests for walk_sequel_chain."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="u1", password="p")
        self.root_item = Item.objects.create(
            media_id="1",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="A1",
            image="img/1",
        )
        # Create as Planning, then flip to Completed via update() to skip
        # the save() hook which would hit the live MAL API.
        self.root = Anime.objects.create(
            item=self.root_item,
            user=self.user,
            status=Status.PLANNING.value,
        )
        Anime.objects.filter(pk=self.root.pk).update(
            status=Status.COMPLETED.value,
        )
        self.root.refresh_from_db()

    def _patch_mal(self, fixtures):
        """Return a context manager patching mal.anime to look up fixtures by id."""

        def side_effect(media_id):
            return fixtures[str(media_id)]

        return patch("app.providers.mal.anime", side_effect=side_effect)

    def test_simple_one_hop_chain(self):
        """root → S2 (untracked) with no further sequels."""
        fixtures = {
            "1": _meta("1", related_anime=[_sequel(2)]),
            "2": _meta("2"),
        }
        with self._patch_mal(fixtures):
            result = walk_sequel_chain(self.user, self.root)

        self.assertIsNotNone(result)
        target, additional, depth, release_status = result
        self.assertEqual(target.media_id, "2")
        self.assertEqual(target.source, Sources.MAL.value)
        self.assertEqual(target.media_type, MediaTypes.ANIME.value)
        self.assertEqual(additional, 0)
        self.assertEqual(depth, 1)
        self.assertEqual(release_status, "Finished")
        # Item was created via get_or_create
        self.assertTrue(
            Item.objects.filter(
                media_id="2",
                source=Sources.MAL.value,
                media_type=MediaTypes.ANIME.value,
            ).exists(),
        )

    def test_multi_hop_chain_counts_additional(self):
        """root → S2 → S3 (both untracked): return S2 with additional=1."""
        fixtures = {
            "1": _meta("1", related_anime=[_sequel(2)]),
            "2": _meta("2", related_anime=[_sequel(3)]),
            "3": _meta("3"),
        }
        with self._patch_mal(fixtures):
            result = walk_sequel_chain(self.user, self.root)

        target, additional, depth, _ = result
        self.assertEqual(target.media_id, "2")
        self.assertEqual(additional, 1)
        self.assertEqual(depth, 1)

    def _track_s2(self, status):
        s2_item = Item.objects.create(
            media_id="2",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="A2",
            image="img/2",
        )
        # Create as Planning, then bypass save() for statuses that would
        # trigger a live MAL API call.
        anime = Anime.objects.create(
            item=s2_item,
            user=self.user,
            status=Status.PLANNING.value,
        )
        if status != Status.PLANNING.value:
            Anime.objects.filter(pk=anime.pk).update(status=status)
        return s2_item

    def test_dedup_hides_in_progress(self):
        """S2 tracked as In Progress → chain stops, no suggestion."""
        self._track_s2(Status.IN_PROGRESS.value)
        fixtures = {
            "1": _meta("1", related_anime=[_sequel(2)]),
            "2": _meta("2", related_anime=[_sequel(3)]),
            "3": _meta("3"),
        }
        with self._patch_mal(fixtures):
            result = walk_sequel_chain(self.user, self.root)
        self.assertIsNone(result)

    def test_dedup_hides_completed(self):
        """S2 tracked as Completed → chain stops."""
        self._track_s2(Status.COMPLETED.value)
        fixtures = {
            "1": _meta("1", related_anime=[_sequel(2)]),
            "2": _meta("2", related_anime=[_sequel(3)]),
            "3": _meta("3"),
        }
        with self._patch_mal(fixtures):
            result = walk_sequel_chain(self.user, self.root)
        self.assertIsNone(result)

    def test_dedup_shows_planning(self):
        """S2 tracked as Planning → visible target is S2 (decision #7)."""
        self._track_s2(Status.PLANNING.value)
        fixtures = {
            "1": _meta("1", related_anime=[_sequel(2)]),
            "2": _meta("2"),
        }
        with self._patch_mal(fixtures):
            result = walk_sequel_chain(self.user, self.root)
        self.assertIsNotNone(result)
        target, _, _, _ = result
        self.assertEqual(target.media_id, "2")

    def test_dedup_shows_paused(self):
        """S2 tracked as Paused → visible target is S2."""
        self._track_s2(Status.PAUSED.value)
        fixtures = {
            "1": _meta("1", related_anime=[_sequel(2)]),
            "2": _meta("2"),
        }
        with self._patch_mal(fixtures):
            result = walk_sequel_chain(self.user, self.root)
        self.assertIsNotNone(result)

    def test_chain_break_skips_dropped(self):
        """S2 tracked as Dropped → skip it and keep walking to S3 (decision #9)."""
        self._track_s2(Status.DROPPED.value)
        fixtures = {
            "1": _meta("1", related_anime=[_sequel(2)]),
            "2": _meta("2", related_anime=[_sequel(3)]),
            "3": _meta("3"),
        }
        with self._patch_mal(fixtures):
            result = walk_sequel_chain(self.user, self.root)
        self.assertIsNotNone(result)
        target, additional, depth, _ = result
        self.assertEqual(target.media_id, "3")
        self.assertEqual(additional, 0)
        self.assertEqual(depth, 2)

    def test_cycle_defense(self):
        """MAL returns 1→2 and 2→1: terminates without infinite loop."""
        fixtures = {
            "1": _meta("1", related_anime=[_sequel(2)]),
            "2": _meta("2", related_anime=[_sequel(1)]),
        }
        with self._patch_mal(fixtures):
            result = walk_sequel_chain(self.user, self.root)
        self.assertIsNotNone(result)
        target, _, _, _ = result
        self.assertEqual(target.media_id, "2")

    def test_max_depth_respected(self):
        """Infinite forward-only chain is bounded by MAX_DEPTH."""
        fixtures = {}
        for i in range(1, MAX_DEPTH + 5):
            fixtures[str(i)] = _meta(
                str(i),
                related_anime=[_sequel(i + 1)],
            )

        with patch(
            "app.providers.mal.anime",
            side_effect=lambda mid: fixtures[str(mid)],
        ) as mock_anime:
            walk_sequel_chain(self.user, self.root)

        # root fetch + up to MAX_DEPTH next-node fetches = MAX_DEPTH + 1
        self.assertLessEqual(mock_anime.call_count, MAX_DEPTH + 1)

    def test_non_sequel_relations_ignored(self):
        """side_story/prequel/recap relations do not create suggestions."""
        fixtures = {
            "1": _meta(
                "1",
                related_anime=[
                    _sequel(2, relation_type="side_story"),
                    _sequel(3, relation_type="prequel"),
                    _sequel(4, relation_type="summary"),
                ],
            ),
        }
        with self._patch_mal(fixtures):
            result = walk_sequel_chain(self.user, self.root)
        self.assertIsNone(result)
        # No Item was created for ids 2/3/4
        for mid in ("2", "3", "4"):
            self.assertFalse(
                Item.objects.filter(
                    media_id=mid,
                    source=Sources.MAL.value,
                    media_type=MediaTypes.ANIME.value,
                ).exists(),
            )

    def test_error_in_mal_propagates(self):
        """If mal.anime raises, walk_sequel_chain re-raises (caller logs/skips)."""
        with patch(
            "app.providers.mal.anime",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                walk_sequel_chain(self.user, self.root)
