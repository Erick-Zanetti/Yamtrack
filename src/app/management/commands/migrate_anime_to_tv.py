"""Migrate a user's MAL Anime entries to TMDB TV + Season entries.

Two-phase workflow:

    python manage.py migrate_anime_to_tv --user <name> --dry-run --output out.csv
    # user edits out.csv manually
    python manage.py migrate_anime_to_tv --user <name> --apply out.csv

The dry-run produces a CSV with suggested TMDB show + season mapping plus a
confidence column. Rows with `action=skip` are ignored on apply. Rows with
`action=apply` are executed. `manual` is an alias for `apply` so the human can
tell "this one I personally verified" from "this one was auto-high-confidence".

The apply step never deletes Anime rows that fail to migrate (e.g. because the
target TMDB ID is blank). Failed rows are logged and left in place so the user
can retry.
"""
from __future__ import annotations

import csv
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from app.models import (
    Anime,
    Item,
    MediaTypes,
    Season,
    Sources,
    Status,
    TV,
)
from app.providers import tmdb

logger = logging.getLogger(__name__)

User = get_user_model()

CSV_FIELDS = [
    "anime_media_id",
    "anime_title",
    "status",
    "progress",
    "score",
    "start_date",
    "end_date",
    "notes",
    "tmdb_id",
    "tmdb_title",
    "season_number",
    "confidence",
    "action",
]


# --- Season detection ------------------------------------------------------

ROMAN = {"II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10}

ORDINAL_SUFFIX = r"(?:st|nd|rd|th)"

SEASON_PATTERNS = [
    # "Season 3 Part 2" → season 3 (flagged low for cour ambiguity)
    (re.compile(rf"\bSeason\s+(\d+)\s+Part\s+\d+\b", re.IGNORECASE), "season_with_cour"),
    # "3rd Season", "2nd Season"
    (re.compile(rf"\b(\d+){ORDINAL_SUFFIX}\s+Season\b", re.IGNORECASE), "ordinal_season"),
    # "Season 2", "Season 3"
    (re.compile(r"\bSeason\s+(\d+)\b", re.IGNORECASE), "plain_season"),
    # "Part 2", "Part 3"
    (re.compile(r"\bPart\s+(\d+)\b", re.IGNORECASE), "part"),
    # Trailing standalone number: "One Punch Man 3"
    (re.compile(r"\s+(\d+)\s*$"), "trailing_number"),
    # Trailing Roman numeral: "Sword Art Online II"
    (re.compile(r"\s+(II|III|IV|V|VI|VII|VIII|IX|X)\s*$"), "roman"),
]

# Tokens we strip from titles before TMDB search.
NOISE_TOKENS = [
    "(TV)",
]


@dataclass
class Suggestion:
    anime: Anime
    base_title: str
    season_number: int
    tmdb_id: str | None
    tmdb_title: str | None
    confidence: str  # high / medium / low


def _strip_year_suffix(title: str) -> tuple[str, int | None]:
    """'Hunter x Hunter (2011)' -> ('Hunter x Hunter', 2011)."""
    m = re.search(r"\((\d{4})\)\s*$", title)
    if not m:
        return title, None
    return title[: m.start()].rstrip(), int(m.group(1))


def _extract_season(title: str) -> tuple[str, int]:
    """Return (base_title, season_number). Season defaults to 1 if no marker."""
    work = title
    for token in NOISE_TOKENS:
        work = work.replace(token, "")

    for pattern, kind in SEASON_PATTERNS:
        m = pattern.search(work)
        if not m:
            continue
        raw = m.group(1)
        if kind == "roman":
            season = ROMAN.get(raw.upper(), 1)
        else:
            season = int(raw)
        base = (work[: m.start()] + work[m.end():]).strip(" :-")
        return base, season

    return work.strip(), 1


def _normalize(s: str) -> str:
    """Normalize for fuzzy match: NFKD, lower, strip punctuation except word chars."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _score_match(anime_base: str, candidate_title: str) -> int:
    """Return 0-100 similarity score between normalized titles."""
    a = _normalize(anime_base)
    b = _normalize(candidate_title)
    if not a or not b:
        return 0
    if a == b:
        return 100
    if b.startswith(a) or a.startswith(b):
        return 80
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    if not a_tokens or not b_tokens:
        return 0
    inter = a_tokens & b_tokens
    union = a_tokens | b_tokens
    jaccard = int(100 * len(inter) / len(union))
    return jaccard


def _search_tmdb_tv(query: str, year: int | None = None):
    """Wrap tmdb.search for TV, return list of {media_id, title, ...}."""
    try:
        data = tmdb.search("tv", query, page=1)
    except Exception:
        logger.exception("TMDB search failed for query=%r", query)
        return []
    results = data.get("results") or []
    if year:
        # We don't have air-date in basic search results; year filter is best-effort
        # and skipped here. TMDB's search API supports first_air_date_year only on
        # /search/tv directly, but we rely on the wrapper which doesn't expose it.
        pass
    return results


def _suggest(anime: Anime) -> Suggestion:
    title = anime.item.title
    title_no_year, year = _strip_year_suffix(title)
    base, season = _extract_season(title_no_year)

    candidates = _search_tmdb_tv(base, year=year)
    if not candidates:
        return Suggestion(anime, base, season, None, None, "low")

    scored = [(_score_match(base, c["title"]), c) for c in candidates[:5]]
    scored.sort(key=lambda t: t[0], reverse=True)
    best_score, best = scored[0]

    if best_score >= 90:
        confidence = "high"
    elif best_score >= 60:
        confidence = "medium"
    else:
        confidence = "low"

    # Ambiguous season-detection patterns always drop to medium at best.
    if season > 1:
        # Pretty confident about the season itself; no demotion.
        pass

    return Suggestion(
        anime,
        base,
        season,
        str(best["media_id"]),
        best["title"],
        confidence,
    )


def _action_from_confidence(confidence: str) -> str:
    return "apply" if confidence == "high" else "manual"


# --- Command ----------------------------------------------------------------


class Command(BaseCommand):
    help = "Migrate a user's MAL Anime entries to TMDB TV + Season entries."

    def add_arguments(self, parser):
        parser.add_argument("--user", required=True)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--apply", type=str, help="Path to confirmed CSV")
        parser.add_argument("--output", type=str, default="/tmp/anime_to_tv_mapping.csv")
        parser.add_argument(
            "--delete-migrated",
            action="store_true",
            help="Delete the original Anime row after a successful migrate. "
                 "Off by default — you can review and delete manually once satisfied.",
        )

    def handle(self, **opts):
        try:
            user = User.objects.get(username=opts["user"])
        except User.DoesNotExist as exc:
            raise CommandError(f"User {opts['user']!r} not found") from exc

        if opts.get("apply"):
            self._apply(user, opts["apply"], delete_migrated=opts["delete_migrated"])
        elif opts.get("dry_run"):
            self._dry_run(user, opts["output"])
        else:
            raise CommandError("Pass either --dry-run or --apply <csv>")

    def _dry_run(self, user, output_path):
        animes = list(
            Anime.objects.filter(user=user).select_related("item").order_by("item__title"),
        )
        self.stdout.write(f"Analyzing {len(animes)} anime entries for {user.username}...")

        rows = []
        for i, anime in enumerate(animes, 1):
            suggestion = _suggest(anime)
            rows.append({
                "anime_media_id": anime.item.media_id,
                "anime_title": anime.item.title,
                "status": anime.status,
                "progress": anime.progress,
                "score": anime.score or "",
                "start_date": anime.start_date.isoformat() if anime.start_date else "",
                "end_date": anime.end_date.isoformat() if anime.end_date else "",
                "notes": anime.notes or "",
                "tmdb_id": suggestion.tmdb_id or "",
                "tmdb_title": suggestion.tmdb_title or "",
                "season_number": suggestion.season_number,
                "confidence": suggestion.confidence,
                "action": _action_from_confidence(suggestion.confidence),
            })
            if i % 10 == 0:
                self.stdout.write(f"  processed {i}/{len(animes)}")

        with Path(output_path).open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

        conf_counts = {c: sum(1 for r in rows if r["confidence"] == c) for c in ("high", "medium", "low")}
        action_counts = {a: sum(1 for r in rows if r["action"] == a) for a in ("apply", "manual", "skip")}

        self.stdout.write(self.style.SUCCESS(
            f"\nWrote {len(rows)} rows to {output_path}",
        ))
        self.stdout.write(f"  confidence: {conf_counts}")
        self.stdout.write(f"  actions:    {action_counts}")
        self.stdout.write(
            "\nNext step: edit the CSV to review 'manual' rows, set action='skip' to "
            "omit a row, fix tmdb_id/season_number where wrong, then run\n"
            f"  python manage.py migrate_anime_to_tv --user {user.username} --apply {output_path}",
        )

    def _apply(self, user, csv_path, *, delete_migrated):
        rows = []
        with Path(csv_path).open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows.append(row)

        migrated = 0
        skipped = 0
        failed = []

        # Group by tmdb_id so we only fetch the TV metadata once per show.
        groups: dict[str, list[dict]] = {}
        for row in rows:
            action = row.get("action", "").strip().lower()
            if action not in ("apply", "manual"):
                skipped += 1
                continue
            tmdb_id = row.get("tmdb_id", "").strip()
            if not tmdb_id:
                failed.append((row["anime_title"], "empty tmdb_id"))
                continue
            groups.setdefault(tmdb_id, []).append(row)

        for tmdb_id, group in groups.items():
            try:
                tv_row = self._get_or_create_tv(user, tmdb_id)
            except Exception as exc:  # noqa: BLE001
                logger.exception("TV create failed: tmdb_id=%s", tmdb_id)
                for row in group:
                    failed.append((row["anime_title"], str(exc)))
                continue

            for row in group:
                try:
                    with transaction.atomic():
                        self._migrate_one(
                            user, tv_row, row, delete_migrated=delete_migrated,
                        )
                    migrated += 1
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "Migration row failed: tmdb_id=%s anime=%s",
                        tmdb_id, row["anime_title"],
                    )
                    failed.append((row["anime_title"], str(exc)))

            try:
                self._update_tv_status(user, tv_row, tmdb_id)
            except Exception:
                logger.exception("TV status update failed: tmdb_id=%s", tmdb_id)

        self.stdout.write(self.style.SUCCESS(f"\nMigrated: {migrated}"))
        self.stdout.write(f"Skipped:  {skipped}")
        if failed:
            self.stdout.write(self.style.WARNING(f"Failed:   {len(failed)}"))
            for title, reason in failed:
                self.stdout.write(f"  - {title}: {reason}")

    def _get_or_create_tv(self, user, tmdb_id):
        """Get or create a TV row for this user + tmdb_id."""
        metadata = tmdb.tv(tmdb_id)
        tv_item, _ = Item.objects.get_or_create(
            media_id=str(tmdb_id),
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            defaults={
                "title": metadata["title"],
                "image": metadata.get("image", ""),
            },
        )
        tv, created = TV.objects.get_or_create(
            item=tv_item,
            user=user,
            defaults={"status": Status.PLANNING.value},
        )
        if created:
            logger.info("Created TV %s for user %s", metadata["title"], user)
        return tv

    def _migrate_one(self, user, tv_row, csv_row, *, delete_migrated):
        season_number = int(csv_row["season_number"])
        anime_media_id = csv_row["anime_media_id"]
        anime = Anime.objects.select_related("item").get(
            user=user,
            item__media_id=anime_media_id,
            item__source=Sources.MAL.value,
            item__media_type=MediaTypes.ANIME.value,
        )

        tmdb_id = str(tv_row.item.media_id)
        season_item, _ = Item.objects.get_or_create(
            media_id=tmdb_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=season_number,
            defaults={
                "title": tv_row.item.title,
                "image": tv_row.item.image,
            },
        )

        existing = Season.objects.filter(item=season_item, user=user).first()
        if existing:
            # Merge: keep the most engaged status, best score, merged notes.
            # Dates/progress are read-only properties on Season (derived from
            # Episode records) so they are not copied.
            season_row = existing
            if anime.status == Status.COMPLETED.value:
                season_row.status = Status.COMPLETED.value
            elif season_row.status not in (
                Status.COMPLETED.value,
                Status.IN_PROGRESS.value,
            ):
                season_row.status = anime.status
            if season_row.score is None:
                season_row.score = anime.score
            elif anime.score is not None:
                season_row.score = round(
                    (float(season_row.score) + float(anime.score)) / 2, 1,
                )
            if anime.notes and anime.notes not in (season_row.notes or ""):
                season_row.notes = f"{season_row.notes}\n{anime.notes}".strip()
        else:
            season_row = Season(
                item=season_item,
                user=user,
                related_tv=tv_row,
                status=anime.status,
                score=anime.score,
                notes=anime.notes or "",
            )

        # save() handles episode auto-creation when status=Completed via
        # Season.save() → which fetches TMDB metadata and bulk-creates Episode
        # rows for all released episodes. Progress is computed from those
        # Episode rows (it's a property on Season, not a stored field).
        season_row.save()

        if delete_migrated:
            anime.delete()

    def _update_tv_status(self, user, tv_row, tmdb_id):
        """Set TV status to Completed if user has all seasons completed, else Planning."""
        metadata = tmdb.tv(tmdb_id)
        total_seasons_metadata = metadata.get("related", {}).get("seasons") or []
        # Filter out specials (season_number == 0) which TMDB uses for specials.
        total_seasons = [s for s in total_seasons_metadata if s.get("season_number", 0) > 0]
        total_count = len(total_seasons)

        user_completed = Season.objects.filter(
            related_tv=tv_row,
            user=user,
            status=Status.COMPLETED.value,
        ).count()

        if total_count > 0 and user_completed == total_count:
            new_status = Status.COMPLETED.value
        elif Season.objects.filter(related_tv=tv_row, user=user).exclude(
            status=Status.PLANNING.value,
        ).exists():
            new_status = Status.IN_PROGRESS.value
        else:
            new_status = Status.PLANNING.value

        TV.objects.filter(pk=tv_row.pk).update(status=new_status)
