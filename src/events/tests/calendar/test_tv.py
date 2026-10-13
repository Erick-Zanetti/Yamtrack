import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import requests
from django.conf import settings
from django.core.cache import cache
from django.test import TestCase

from app.models import TV, Episode, Item, MediaTypes, Season, Status
from events.calendar.helpers import date_parser
from events.calendar.tv import (
    get_episode_datetime,
    get_seasons_to_process,
    get_tvmaze_episode_map,
    get_tvmaze_response,
    process_season_episodes,
    process_tv,
    process_tv_seasons,
)
from events.models import Event
from events.tests.calendar.utils import CalendarFixturesMixin


class CalendarTVTests(CalendarFixturesMixin, TestCase):
    """Test TV calendar processing."""

    @patch("events.calendar.tv.tmdb.tv")
    @patch("events.calendar.tv.tmdb.tv_with_seasons")
    @patch("events.calendar.tv.get_tvmaze_episode_map")
    def test_process_tv_season(
        self,
        mock_get_tvmaze_episode_map,
        mock_tv_with_seasons,
        mock_tv,
    ):
        """Test processing for a TV season."""
        mock_tv.return_value = {
            "related": {
                "seasons": [
                    {"season_number": 1, "episodes": [1, 2, 3]},
                    {"season_number": 2, "episodes": [1, 2]},
                    {"season_number": 3, "episodes": [1]},
                ],
            },
            "next_episode_season": 2,
        }

        mock_tv_with_seasons.return_value = {
            "season/1": {
                "image": "http://example.com/season1.jpg",
                "season_number": 1,
                "episodes": [
                    {"episode_number": 1, "air_date": "2008-01-20"},
                    {"episode_number": 2, "air_date": "2008-01-27"},
                    {"episode_number": 3, "air_date": "2008-02-03"},
                ],
                "tvdb_id": "81189",
            },
            "season/2": {
                "image": "http://example.com/season2.jpg",
                "season_number": 2,
                "episodes": [
                    {"episode_number": 1, "air_date": "2009-01-20"},
                    {"episode_number": 2, "air_date": "2009-01-27"},
                ],
                "tvdb_id": "81189",
            },
            "season/3": {
                "image": "http://example.com/season3.jpg",
                "season_number": 3,
                "episodes": [
                    {"episode_number": 1, "air_date": "2010-01-20"},
                ],
                "tvdb_id": "81189",
            },
        }

        mock_get_tvmaze_episode_map.return_value = {
            "1_1": "2008-01-20T22:00:00+00:00",
            "1_2": "2008-01-27T22:00:00+00:00",
            "1_3": "2008-02-03T22:00:00+00:00",
        }

        events_bulk = []
        process_tv(self.tv_item, events_bulk)

        self.assertEqual(len(events_bulk), 6)
        self.assertEqual(events_bulk[0].item, self.season_item)
        self.assertEqual(events_bulk[0].content_number, 1)

        expected_date = datetime.datetime.fromisoformat("2008-01-20T22:00:00+00:00")
        self.assertEqual(events_bulk[0].datetime, expected_date)

    @patch("events.calendar.tv.get_tvmaze_episode_map")
    @patch("events.calendar.tv.tmdb.tv_with_seasons")
    @patch("events.calendar.tv.tmdb.tv")
    def test_process_tv_reopens_completed_show_with_new_season_as_planning(
        self,
        mock_tv,
        mock_tv_with_seasons,
        mock_get_tvmaze_episode_map,
    ):
        """Completed TV should reopen and create the discovered season as planning."""
        TV.objects.filter(item=self.tv_item, user=self.user).update(
            status=Status.COMPLETED.value,
        )
        Season.objects.filter(item=self.season_item, user=self.user).update(
            status=Status.COMPLETED.value,
        )
        Event.objects.create(
            item=self.season_item,
            content_number=1,
            datetime=date_parser("2008-01-20"),
        )

        mock_tv.return_value = {
            "related": {
                "seasons": [
                    {"season_number": 1, "episodes": [1]},
                    {"season_number": 2, "episodes": [1]},
                ],
            },
            "next_episode_season": 2,
        }
        mock_tv_with_seasons.return_value = {
            "season/2": {
                "image": "http://example.com/season2.jpg",
                "season_number": 2,
                "episodes": [
                    {"episode_number": 1, "air_date": "2027-01-20"},
                ],
                "tvdb_id": "81189",
            },
        }
        mock_get_tvmaze_episode_map.return_value = {}

        events_bulk = []
        process_tv(self.tv_item, events_bulk)

        season_two_item = Item.objects.get(
            media_id=self.tv_item.media_id,
            source=self.tv_item.source,
            media_type=self.season_item.media_type,
            season_number=2,
        )
        season_two = Season.objects.get(item=season_two_item, user=self.user)
        tv = TV.objects.get(item=self.tv_item, user=self.user)

        self.assertEqual(tv.status, Status.IN_PROGRESS.value)
        self.assertEqual(season_two.status, Status.PLANNING.value)
        self.assertEqual(len(events_bulk), 1)

    @patch("events.calendar.tv.get_tvmaze_episode_map")
    @patch("events.calendar.tv.tmdb.tv_with_seasons")
    @patch("events.calendar.tv.tmdb.tv")
    def test_process_tv_does_not_reopen_completed_show_for_past_only_season(
        self,
        mock_tv,
        mock_tv_with_seasons,
        mock_get_tvmaze_episode_map,
    ):
        """Past-only seasons should not reopen a completed TV entry."""
        TV.objects.filter(item=self.tv_item, user=self.user).update(
            status=Status.COMPLETED.value,
        )
        Season.objects.filter(item=self.season_item, user=self.user).update(
            status=Status.COMPLETED.value,
        )
        Event.objects.create(
            item=self.season_item,
            content_number=1,
            datetime=date_parser("2008-01-20"),
        )

        mock_tv.return_value = {
            "related": {
                "seasons": [
                    {"season_number": 1, "episodes": [1]},
                    {"season_number": 2, "episodes": [1]},
                ],
            },
            "next_episode_season": 2,
        }
        mock_tv_with_seasons.return_value = {
            "season/2": {
                "image": "http://example.com/season2.jpg",
                "season_number": 2,
                "episodes": [
                    {"episode_number": 1, "air_date": "2010-01-20"},
                ],
                "tvdb_id": "81189",
            },
        }
        mock_get_tvmaze_episode_map.return_value = {}

        events_bulk = []
        process_tv(self.tv_item, events_bulk)

        tv = TV.objects.get(item=self.tv_item, user=self.user)
        self.assertEqual(tv.status, Status.COMPLETED.value)
        self.assertFalse(
            Season.objects.filter(
                item__media_id=self.tv_item.media_id,
                item__source=self.tv_item.source,
                item__season_number=2,
                user=self.user,
            ).exists(),
        )
        self.assertEqual(len(events_bulk), 1)

    def _complete_season_one_with_watched_episodes(self, episode_numbers):
        TV.objects.filter(item=self.tv_item, user=self.user).update(
            status=Status.COMPLETED.value,
        )
        Season.objects.filter(item=self.season_item, user=self.user).update(
            status=Status.COMPLETED.value,
        )
        season = Season.objects.get(item=self.season_item, user=self.user)
        Episode.objects.bulk_create(
            [
                Episode(
                    item=Item.objects.create(
                        media_id=self.tv_item.media_id,
                        source=self.tv_item.source,
                        media_type=MediaTypes.EPISODE.value,
                        title=self.tv_item.title,
                        season_number=1,
                        episode_number=episode_number,
                    ),
                    related_season=season,
                    end_date=date_parser("2008-02-01"),
                )
                for episode_number in episode_numbers
            ],
        )

    @staticmethod
    def _season_one_metadata(episodes):
        return {
            "season/1": {
                "image": "http://example.com/season1.jpg",
                "season_number": 1,
                "episodes": episodes,
                "tvdb_id": "81189",
            },
        }

    @patch("events.calendar.tv.get_tvmaze_episode_map")
    @patch("events.calendar.tv.tmdb.tv_with_seasons")
    @patch("events.calendar.tv.tmdb.tv")
    def test_process_tv_reopens_completed_show_when_tracked_season_grows(
        self,
        mock_tv,
        mock_tv_with_seasons,
        mock_get_tvmaze_episode_map,
    ):
        """A new cour appended to an already-tracked season reopens the show.

        TMDB sometimes keeps a show in a single season and appends the new
        episodes to it (e.g. Ranma1/2), so no new season number ever appears.
        """
        self._complete_season_one_with_watched_episodes([1])
        mock_tv.return_value = {
            "related": {"seasons": [{"season_number": 1, "episodes": [1, 2]}]},
            "next_episode_season": 1,
        }
        mock_tv_with_seasons.return_value = self._season_one_metadata(
            [
                {"episode_number": 1, "air_date": "2008-01-20"},
                {"episode_number": 2, "air_date": "2027-01-20"},
            ],
        )
        mock_get_tvmaze_episode_map.return_value = {}

        events_bulk = []
        process_tv(self.tv_item, events_bulk)

        tv = TV.objects.get(item=self.tv_item, user=self.user)
        season_one = Season.objects.get(item=self.season_item, user=self.user)
        self.assertEqual(tv.status, Status.IN_PROGRESS.value)
        self.assertEqual(season_one.status, Status.IN_PROGRESS.value)
        self.assertEqual(Season.objects.filter(user=self.user).count(), 1)

    @patch("events.calendar.tv.get_tvmaze_episode_map")
    @patch("events.calendar.tv.tmdb.tv_with_seasons")
    @patch("events.calendar.tv.tmdb.tv")
    def test_process_tv_reopens_unwatched_completed_season_as_planning(
        self,
        mock_tv,
        mock_tv_with_seasons,
        mock_get_tvmaze_episode_map,
    ):
        """A season marked completed without watched episodes goes to planning."""
        self._complete_season_one_with_watched_episodes([])
        mock_tv.return_value = {
            "related": {"seasons": [{"season_number": 1, "episodes": [1]}]},
            "next_episode_season": 1,
        }
        mock_tv_with_seasons.return_value = self._season_one_metadata(
            [{"episode_number": 1, "air_date": "2027-01-20"}],
        )
        mock_get_tvmaze_episode_map.return_value = {}

        process_tv(self.tv_item, [])

        tv = TV.objects.get(item=self.tv_item, user=self.user)
        season_one = Season.objects.get(item=self.season_item, user=self.user)
        self.assertEqual(tv.status, Status.IN_PROGRESS.value)
        self.assertEqual(season_one.status, Status.PLANNING.value)

    @patch("events.calendar.tv.get_tvmaze_episode_map")
    @patch("events.calendar.tv.tmdb.tv_with_seasons")
    @patch("events.calendar.tv.tmdb.tv")
    def test_process_tv_keeps_completed_when_future_episode_already_watched(
        self,
        mock_tv,
        mock_tv_with_seasons,
        mock_get_tvmaze_episode_map,
    ):
        """A wrong future date on an episode already watched must not reopen."""
        self._complete_season_one_with_watched_episodes([1, 2])
        mock_tv.return_value = {
            "related": {"seasons": [{"season_number": 1, "episodes": [1, 2]}]},
            "next_episode_season": 1,
        }
        mock_tv_with_seasons.return_value = self._season_one_metadata(
            [
                {"episode_number": 1, "air_date": "2008-01-20"},
                {"episode_number": 2, "air_date": "2027-01-20"},
            ],
        )
        mock_get_tvmaze_episode_map.return_value = {}

        events_bulk = []
        process_tv(self.tv_item, events_bulk)

        tv = TV.objects.get(item=self.tv_item, user=self.user)
        season_one = Season.objects.get(item=self.season_item, user=self.user)
        self.assertEqual(tv.status, Status.COMPLETED.value)
        self.assertEqual(season_one.status, Status.COMPLETED.value)

    @patch("events.calendar.tv.services.api_request")
    def test_get_tvmaze_episode_map(self, mock_api_request):
        """Test get_tvmaze_episode_map function."""
        cache.clear()

        mock_api_request.side_effect = [
            {"id": 12345},
            {
                "_embedded": {
                    "episodes": [
                        {
                            "season": 1,
                            "number": 1,
                            "airstamp": "2008-01-20T22:00:00+00:00",
                            "airtime": "22:00",
                        },
                        {
                            "season": 1,
                            "number": 2,
                            "airstamp": "2008-01-27T22:00:00+00:00",
                            "airtime": "22:00",
                        },
                    ],
                },
            },
        ]

        result = get_tvmaze_episode_map("81189")

        self.assertEqual(len(result), 2)
        self.assertIn("1_1", result)
        self.assertIn("1_2", result)
        self.assertEqual(result["1_1"], "2008-01-20T22:00:00+00:00")
        self.assertEqual(result["1_2"], "2008-01-27T22:00:00+00:00")

        cached_result = cache.get("tvmaze_map_81189")
        self.assertEqual(cached_result, result)

        mock_api_request.reset_mock()
        get_tvmaze_episode_map("81189")
        mock_api_request.assert_not_called()

    @patch("events.calendar.tv.services.api_request")
    def test_get_tvmaze_episode_map_lookup_failure(self, mock_api_request):
        """Test get_tvmaze_episode_map when lookup fails."""
        cache.clear()
        mock_api_request.return_value = None

        result = get_tvmaze_episode_map("invalid_id")

        self.assertEqual(result, {})
        mock_api_request.assert_called_once()

    def test_get_episode_datetime_falls_back_to_tmdb_air_date(self):
        """TMDB dates should be used when TVMaze has no timestamp."""
        result = get_episode_datetime(
            {"air_date": "2025-01-31"},
            season_number=1,
            episode_number=2,
            tvmaze_map={},
        )

        self.assertEqual(result, date_parser("2025-01-31"))

    def test_get_episode_datetime_returns_placeholder_for_invalid_date(self):
        """Invalid or missing episode dates should produce a placeholder datetime."""
        result = get_episode_datetime(
            {"air_date": "not-a-date"},
            season_number=1,
            episode_number=2,
            tvmaze_map={},
        )

        self.assertEqual(result, datetime.datetime.min.replace(tzinfo=ZoneInfo("UTC")))

    @patch("events.calendar.tv.services.api_request")
    def test_get_tvmaze_response_returns_empty_on_not_found(self, mock_api_request):
        """A 404 from the TVMaze lookup should be tolerated."""
        response = MagicMock()
        response.status_code = requests.codes.not_found
        response.text = "missing"
        mock_api_request.side_effect = requests.exceptions.HTTPError(response=response)

        self.assertEqual(get_tvmaze_response("999"), {})

    @patch("events.calendar.tv.tmdb.tv")
    def test_get_seasons_to_process_returns_empty_when_no_seasons(self, mock_tv):
        """TV metadata without seasons should short-circuit processing."""
        mock_tv.return_value = {"related": {"seasons": []}}

        self.assertEqual(get_seasons_to_process(self.tv_item), [])

    @patch("events.calendar.tv.get_seasons_to_process")
    def test_process_tv_returns_when_no_seasons_need_processing(
        self,
        mock_get_seasons_to_process,
    ):
        """process_tv should stop cleanly when there is nothing new to fetch."""
        mock_get_seasons_to_process.return_value = []

        events_bulk = []
        process_tv(self.tv_item, events_bulk)

        self.assertEqual(events_bulk, [])

    def test_process_season_episodes_handles_missing_tvdb_and_episodes(self):
        """A season without TVDB data or episodes should not add events."""
        events_bulk = []
        process_season_episodes(
            self.season_item,
            {
                "season_number": 1,
                "episodes": [],
            },
            events_bulk,
        )

        self.assertEqual(events_bulk, [])

    @patch("events.calendar.tv.services.api_request")
    def test_get_tvmaze_response_returns_empty_when_lookup_has_no_id(
        self,
        mock_api_request,
    ):
        """Lookup responses without a TVMaze id should return an empty mapping."""
        mock_api_request.return_value = {"name": "Breaking Bad"}

        self.assertEqual(get_tvmaze_response("81189"), {})

    @patch("events.calendar.tv.services.api_request")
    def test_get_tvmaze_response_returns_empty_when_episode_fetch_fails(
        self,
        mock_api_request,
    ):
        """Episode fetch errors after a successful lookup should be tolerated."""
        response = MagicMock()
        response.status_code = 500
        response.text = "boom"
        mock_api_request.side_effect = [
            {"id": 12345},
            requests.exceptions.HTTPError(response=response),
        ]

        self.assertEqual(get_tvmaze_response("81189"), {})

    @patch("events.calendar.tv.get_tvmaze_episode_map")
    def test_process_season_episodes_ignores_tvmaze_when_season_numbering_differs(
        self,
        mock_get_tvmaze_episode_map,
    ):
        """TVMaze dates that disagree with TMDB for a season must be discarded.

        TVMaze and TMDB number seasons differently for some shows (Futurama's
        TVMaze S11 is the 2023 revival, TMDB's S11 airs in 2026), so a matching
        "season_episode" key can point at a completely different season.
        """
        mock_get_tvmaze_episode_map.return_value = {
            "11_1": "2023-07-24T16:00:00+00:00",
            "11_2": "2023-07-31T16:00:00+00:00",
            "11_3": "2023-08-07T16:00:00+00:00",
        }

        events_bulk = []
        process_season_episodes(
            self.season_item,
            {
                "season_number": 11,
                "tvdb_id": "73871",
                "episodes": [
                    {"episode_number": 1, "air_date": "2026-08-03"},
                    {"episode_number": 2, "air_date": "2026-08-03"},
                    {"episode_number": 3, "air_date": None},
                ],
            },
            events_bulk,
        )

        self.assertEqual(
            [event.datetime for event in events_bulk],
            [
                date_parser("2026-08-03"),
                date_parser("2026-08-03"),
                datetime.datetime.min.replace(tzinfo=ZoneInfo("UTC")),
            ],
        )

    @patch("events.calendar.tv.get_tvmaze_episode_map")
    def test_process_season_episodes_keeps_tvmaze_when_dates_agree(
        self,
        mock_get_tvmaze_episode_map,
    ):
        """Agreeing TVMaze timestamps are kept, even for TMDB-undated episodes."""
        mock_get_tvmaze_episode_map.return_value = {
            # US evening airing lands on the next UTC day: still within tolerance.
            "1_1": "2026-08-03T03:30:00+00:00",
            "1_2": "2026-08-10T03:30:00+00:00",
            "1_3": "2026-08-17T03:30:00+00:00",
        }

        events_bulk = []
        process_season_episodes(
            self.season_item,
            {
                "season_number": 1,
                "tvdb_id": "81189",
                "episodes": [
                    {"episode_number": 1, "air_date": "2026-08-02"},
                    {"episode_number": 2, "air_date": "2026-08-10"},
                    {"episode_number": 3, "air_date": None},
                ],
            },
            events_bulk,
        )

        self.assertEqual(
            [event.datetime for event in events_bulk],
            [
                datetime.datetime.fromisoformat("2026-08-03T03:30:00+00:00"),
                datetime.datetime.fromisoformat("2026-08-10T03:30:00+00:00"),
                datetime.datetime.fromisoformat("2026-08-17T03:30:00+00:00"),
            ],
        )

    @patch("events.calendar.tv.tmdb.tv_with_seasons")
    def test_process_tv_seasons_refreshes_placeholder_season_image(
        self,
        mock_tv_with_seasons,
    ):
        """A season stored with the placeholder poster picks up TMDB's real one."""
        self.season_item.image = settings.IMG_NONE
        self.season_item.save()
        mock_tv_with_seasons.return_value = {
            "season/1": {
                "image": "http://example.com/real-poster.jpg",
                "season_number": 1,
                "episodes": [],
            },
        }

        process_tv_seasons(self.tv_item, [1], [])

        self.season_item.refresh_from_db()
        self.assertEqual(self.season_item.image, "http://example.com/real-poster.jpg")

    @patch("events.calendar.tv.tmdb.tv_with_seasons")
    def test_process_tv_seasons_keeps_existing_season_image(
        self,
        mock_tv_with_seasons,
    ):
        """A season that already has a real poster is left untouched."""
        mock_tv_with_seasons.return_value = {
            "season/1": {
                "image": "http://example.com/other-poster.jpg",
                "season_number": 1,
                "episodes": [],
            },
        }

        process_tv_seasons(self.tv_item, [1], [])

        self.season_item.refresh_from_db()
        self.assertEqual(self.season_item.image, "http://example.com/breakingbad.jpg")
