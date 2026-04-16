"""Reconcile episode counts after migrate_anime_to_tv.

For each migrated Season, compare episode count against the original MAL
anime progress. Two cases:

1. Shrink: eps_created > mal_progress → delete excess episodes and recompute
   Season status. Typically caused by TMDB merging MAL split cours into one
   season (e.g. Aldnoah, Gate, Mashle).

2. Expand (not fixed here, only reported): eps_created < mal_progress →
   user watched more than the TMDB season covers. Typically caused by
   long-running shows where MAL has one entry but TMDB has many seasons
   (One Piece, Naruto, Dragon Ball, Fairy Tail). User must manually fill.
"""
from __future__ import annotations

import csv
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from app.models import (
    Episode,
    MediaTypes,
    Season,
    Sources,
    Status,
)

User = get_user_model()


class Command(BaseCommand):
    help = "Post-migration reconciliation: trim excess episodes, report gaps."

    def add_arguments(self, parser):
        parser.add_argument("--user", required=True)
        parser.add_argument("--csv", required=True, help="Path to migration mapping CSV")
        parser.add_argument("--apply", action="store_true", help="Actually trim episodes; default is dry-run")

    def handle(self, **opts):
        try:
            user = User.objects.get(username=opts["user"])
        except User.DoesNotExist as exc:
            raise CommandError(f"User not found: {opts['user']}") from exc

        rows = []
        with Path(opts["csv"]).open() as f:
            for row in csv.DictReader(f):
                if row.get("action") == "apply":
                    rows.append(row)

        # Group by destination season — several MAL entries may map to the same
        # TMDB season (e.g. Shingeki S3 + S3 Part 2 both land on AoT S3).
        by_slot: dict[tuple[str, int], dict] = {}
        for row in rows:
            key = (row["tmdb_id"], int(row["season_number"]))
            entry = by_slot.setdefault(
                key, {"titles": [], "sum_progress": 0, "first_row": row},
            )
            entry["titles"].append(row["anime_title"])
            entry["sum_progress"] += int(row["progress"])

        shrinks = []
        expands = []
        ok = 0

        for (tmdb_id, season_num), entry in by_slot.items():
            season = Season.objects.filter(
                user=user,
                item__media_id=tmdb_id,
                item__source=Sources.TMDB.value,
                item__season_number=season_num,
                item__media_type=MediaTypes.SEASON.value,
            ).first()
            if not season:
                continue
            eps = Episode.objects.filter(related_season=season).order_by(
                "item__episode_number",
            )
            ep_count = eps.count()
            sum_progress = entry["sum_progress"]
            titles = " + ".join(entry["titles"])
            if ep_count == sum_progress:
                ok += 1
            elif ep_count > sum_progress:
                shrinks.append((titles, season, list(eps), sum_progress))
            else:
                expands.append((titles, season, ep_count, sum_progress))

        self.stdout.write(f"\nTotal migrations reviewed: {len(rows)}")
        self.stdout.write(f"  OK (eps == MAL progress):           {ok}")
        self.stdout.write(f"  Shrinks (TMDB merged cours):       {len(shrinks)}")
        self.stdout.write(f"  Expands (TMDB split into seasons): {len(expands)}")

        if shrinks:
            self.stdout.write("\n--- Shrinks (will trim excess episodes) ---")
            for titles, season, eps, sp in shrinks:
                self.stdout.write(
                    f"  {titles}: trim {season}: {len(eps)} → {sp} eps",
                )

        if expands:
            self.stdout.write("\n--- Expands (manual review; not changed by this script) ---")
            for titles, season, ec, sp in expands:
                self.stdout.write(
                    f"  {titles}: {season} has {ec} eps, MAL had {sp}",
                )

        if not opts["apply"]:
            self.stdout.write(
                self.style.WARNING("\nDry-run only. Pass --apply to perform the trims."),
            )
            return

        self.stdout.write("\nApplying shrinks...")
        for titles, season, eps, sp in shrinks:
            to_delete = eps[sp:]
            for ep in to_delete:
                ep.delete()
            remaining_count = Episode.objects.filter(related_season=season).count()
            if remaining_count == 0:
                season.status = Status.PLANNING.value
            else:
                season.status = Status.IN_PROGRESS.value
            season.save(update_fields=["status"])
        self.stdout.write(self.style.SUCCESS(f"Trimmed {len(shrinks)} seasons."))
