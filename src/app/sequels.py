"""Anime sequel chain walker.

Given a completed anime, follow the MAL sequel chain and return the first
sequel the user should be offered to track. Rules are defined in the design
doc (see docs/superpowers/specs/2026-04-16-anime-sequel-suggestions-design.md).

Contract: if mal.anime raises, the exception propagates — the caller (the
Celery task) is responsible for logging and skipping.
"""
from __future__ import annotations

from app import providers
from app.models import Anime, Item, MediaTypes, Sources, Status

SEQUEL_RELATION = "sequel"
CHAIN_STOP_STATUSES = frozenset({
    Status.IN_PROGRESS.value,
    Status.COMPLETED.value,
})
VISIBLE_TRACKED_STATUSES = frozenset({
    Status.PLANNING.value,
    Status.PAUSED.value,
})
MAX_DEPTH = 10


def _get_tracked_status(user, media_id):
    """Return the status string the user tracks this MAL anime with, or None."""
    return (
        Anime.objects
        .filter(
            user=user,
            item__media_id=str(media_id),
            item__source=Sources.MAL.value,
            item__media_type=MediaTypes.ANIME.value,
        )
        .values_list("status", flat=True)
        .first()
    )


def walk_sequel_chain(user, root_anime):
    """Walk the sequel chain from a completed anime and return the next suggestion.

    Returns a 4-tuple (target_item, additional_count, chain_depth, release_status)
    when a visible target is found, or None.
    """
    root_id = str(root_anime.item.media_id)
    current_metadata = providers.mal.anime(root_id)
    visited = {root_id}

    visible_target_id = None
    visible_target_title = None
    visible_target_image = None
    visible_release_status = None
    visible_depth = None
    additional = 0
    depth = 0

    while depth < MAX_DEPTH:
        sequels = [
            entry
            for entry in current_metadata["related"]["related_anime"]
            if entry.get("relation_type") == SEQUEL_RELATION
        ]
        if not sequels:
            break

        next_entry = sequels[0]
        next_id = str(next_entry["media_id"])
        if next_id in visited:
            break
        visited.add(next_id)
        depth += 1

        tracked_status = _get_tracked_status(user, next_id)
        if tracked_status in CHAIN_STOP_STATUSES:
            break

        next_metadata = providers.mal.anime(next_id)

        if tracked_status == Status.DROPPED.value:
            current_metadata = next_metadata
            continue

        if visible_target_id is None:
            visible_target_id = next_id
            visible_target_title = next_entry.get("title") or f"Anime {next_id}"
            visible_target_image = next_entry.get("image") or ""
            visible_release_status = next_metadata.get("details", {}).get("status", "")
            visible_depth = depth
        else:
            additional += 1

        current_metadata = next_metadata

    if visible_target_id is None:
        return None

    target_item, _ = Item.objects.get_or_create(
        media_id=visible_target_id,
        source=Sources.MAL.value,
        media_type=MediaTypes.ANIME.value,
        defaults={
            "title": visible_target_title,
            "image": visible_target_image,
        },
    )
    return (target_item, additional, visible_depth, visible_release_status)
