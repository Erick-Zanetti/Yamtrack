"""Mark a range of TV seasons as Completed for a user.

Usage:
    python manage.py complete_tv_seasons \\
        --user <username> --tmdb-id 37854 --seasons 2-22

Seasons can be "2-22" (inclusive range), or "2,3,5-8" (mixed).
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from app.models import Item, MediaTypes, Season, Sources, Status, TV


def _parse_seasons(arg: str) -> list[int]:
    seasons: set[int] = set()
    for part in arg.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            seasons.update(range(int(a), int(b) + 1))
        else:
            seasons.add(int(part))
    return sorted(seasons)


class Command(BaseCommand):
    help = "Mark a range of TV seasons as Completed."

    def add_arguments(self, parser):
        parser.add_argument("--user", required=True)
        parser.add_argument("--tmdb-id", required=True)
        parser.add_argument("--seasons", required=True,
                            help='e.g. "2-22" or "2,3,5-8"')

    def handle(self, **opts):
        User = get_user_model()
        try:
            user = User.objects.get(username=opts["user"])
        except User.DoesNotExist as exc:
            raise CommandError(f"User not found: {opts['user']}") from exc

        try:
            tv = TV.objects.get(
                user=user,
                item__media_id=opts["tmdb_id"],
                item__source=Sources.TMDB.value,
            )
        except TV.DoesNotExist as exc:
            raise CommandError(
                f"TV not found for user={user} tmdb_id={opts['tmdb_id']}",
            ) from exc

        seasons = _parse_seasons(opts["seasons"])
        self.stdout.write(f"TV: {tv.item.title}")
        self.stdout.write(f"Marking seasons as Completed: {seasons}")

        for sn in seasons:
            season_item, _ = Item.objects.get_or_create(
                media_id=opts["tmdb_id"],
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                season_number=sn,
                defaults={"title": tv.item.title, "image": tv.item.image},
            )
            existing = Season.objects.filter(item=season_item, user=user).first()
            if existing:
                if existing.status == Status.COMPLETED.value:
                    action = "already-completed"
                else:
                    existing.status = Status.COMPLETED.value
                    existing.save()
                    action = "updated"
            else:
                season = Season(
                    item=season_item,
                    user=user,
                    related_tv=tv,
                    status=Status.COMPLETED.value,
                )
                season.save()
                action = "created"
            self.stdout.write(f"  S{sn:2d}: {action}")

        tv.refresh_from_db()
        self.stdout.write(self.style.SUCCESS(f"\nTV status: {tv.status}"))
