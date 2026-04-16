# Anime Sequel Suggestions — Design

**Date:** 2026-04-16
**Status:** Design — pending user approval
**Scope:** New feature for the `app` Django app.

---

## 1. Problem

Yamtrack models anime as flat entries (no TV-like season hierarchy): each MyAnimeList (MAL) anime ID is its own `Anime` row. Sequels, cours, and split-season releases (e.g. *Aldnoah.Zero* and *Aldnoah.Zero Part 2*, *Ansatsu Kyoushitsu* and *Ansatsu Kyoushitsu 2nd Season*) are **separate catalog entries** from MAL's point of view.

After marking an anime as `Completed`, the user has no surface that tells them "this show you finished already has a next part — you aren't tracking it." Sequels are only visible on the detail page of the original anime, which the user has no reason to revisit.

## 2. Goal

Surface sequels of completed anime that the user has not yet added, on the existing home page, so the user can decide whether to start tracking them.

## 3. Non-goals

- Manga sequels (`related_manga`).
- Non-sequel relations (side story, spin-off, recap, alternative version).
- Movie / TV sequels.
- Apprise push notifications when a new sequel is detected.
- Filtering by release date (upcoming vs already aired).
- A dismiss-undo UI (dismiss is permanent by design).

These are explicitly deferred and may become follow-ups.

## 4. Behavioral decisions

All decisions were confirmed with the user in the brainstorming session:

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Trigger: anime with status `Completed` only. | The user's mental model: "I finished it, what's next?" |
| 2 | Relation type: `sequel` only (not `side_story`, `prequel`, `spin_off`, etc.). | Minimize noise; covers the split-cour case exactly. |
| 3 | Surface: new section on home between `In Progress` and `Planning`. | Matches where the user already scans for what to watch next. |
| 4 | Card actions: **View details**, **Add to Planning**, **Ignore**. | Give both "explore" and "1-click commit" paths. |
| 5 | Release-date filter: none — show released and upcoming. Status badge on the card. | User wants full visibility. |
| 6 | Detection: Celery periodic task, daily. | Matches existing Yamtrack pattern (releases are also precomputed); cache stays warm via existing MAL cache. |
| 7 | Library dedup: hide suggestion if target is tracked as `In Progress`, `Completed`, or `Dropped`. Show if target is not tracked, or tracked as `Planning` or `Paused`. | `In Progress`/`Completed`/`Dropped` mean the user already has an opinion on that specific entry. |
| 8 | Chain traversal: one card per completed-root, `+N more` badge when deeper sequels exist. | Avoids repeating the same franchise multiple times. |
| 9 | Chain-break rule: stop at `In Progress` or `Completed`; skip over `Dropped` and keep walking. | `In Progress`/`Completed` imply active engagement with the franchise; `Dropped` is local to that specific entry. |

## 5. Architecture

Three new pieces, all in the `app` Django app:

1. **Model** `app.models.SequelSuggestion` — one row per `(user, completed_root_anime, suggested_target_item)`. Source of truth for what the home page renders.
2. **Module** `app.sequels` — contains `walk_sequel_chain(user, root_anime)` which traverses the MAL sequel chain applying the rules from section 4.
3. **Celery task** `app.tasks.refresh_all_sequel_suggestions` + per-user variant — daily job that rebuilds `SequelSuggestion` rows.

Rendering flow (home GET is just a DB query, no API calls at request time):

```
GET /home
  └─ views.home()
       ├─ existing In Progress section
       ├─ NEW Sequels Available section  ← SELECT on SequelSuggestion
       └─ existing Planning section
```

Refresh flow:

```
celery-beat @ daily 04:00 UTC
  └─ refresh_all_sequel_suggestions
       └─ for each user: refresh_sequel_suggestions_for_user.delay(user_id)
            └─ for each Completed Anime:
                 walk_sequel_chain(user, anime)  (uses MAL 24h cache)
                 update_or_create SequelSuggestion(...)
            └─ garbage-collect stale rows for this user
```

Plus Django signals on `Anime.post_save` to invalidate suggestions without waiting for the next daily refresh when statuses change.

## 6. Data model

New model added to `src/app/models.py`:

```python
class SequelSuggestion(models.Model):
    """Suggested next anime based on a completed anime's sequel chain."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sequel_suggestions",
    )
    root = models.ForeignKey(
        "Anime",
        on_delete=models.CASCADE,
        related_name="sequel_suggestions",
    )
    target = models.ForeignKey(
        "Item",
        on_delete=models.CASCADE,
        related_name="suggested_as_sequel",
    )
    additional_count = models.PositiveIntegerField(default=0)
    chain_depth = models.PositiveIntegerField(default=1)
    target_release_status = models.CharField(max_length=20, blank=True)
    dismissed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    refreshed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=["user", "root", "target"],
                name="unique_sequel_suggestion_per_user",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "dismissed_at"]),
        ]
        ordering = ["-refreshed_at"]
```

**Field notes:**
- `root` → `Anime` (user's tracked instance). `target` → `Item` (catalog). This lets us suggest something the user has **not added** — `Item` is the universal catalog entity, while `Anime` is the per-user tracked instance.
- `target` may need to be created by the task before saving the suggestion. Use `Item.objects.get_or_create(media_id, source="mal", media_type="anime", defaults=...)` — same pattern used by `Item.fetch_releases()`.
- `additional_count` implements the `+N more` badge (decision #8).
- `dismissed_at` is a permanent soft-flag; re-runs of the Celery task preserve it.
- `unique_together(user, root, target)` + `update_or_create` prevents duplicates.

**Migration:** one new Django migration adding the model. No data backfill.

## 7. Chain-walking logic

New module `src/app/sequels.py`.

### 7.1. Exposing `relation_type` from the MAL provider

Minimal change in `src/app/providers/mal.py:384` — keep `relation_type` in the returned dict:

```python
def get_related(related_medias, media_type):
    if related_medias:
        return [
            {
                "media_id": media["node"]["id"],
                "source": Sources.MAL.value,
                "title": media["node"]["title"],
                "media_type": media_type,
                "image": get_image_url(media["node"]),
                "relation_type": media.get("relation_type"),  # NEW
            }
            for media in related_medias
        ]
    return []
```

The existing MAL response cache (24h, `cache.set(cache_key, data)` in `mal.py:88`) is reused — no additional API calls introduced.

### 7.2. `walk_sequel_chain(user, root_anime)`

Returns `(target_item, additional_count, chain_depth, release_status)` or `None`.

Algorithm:

```
current = root_anime.item.media_id
visited = {current}
visible_target = None
additional = 0
depth = 0

while depth < MAX_DEPTH (=10):
    metadata = mal.anime(current)            # cached
    sequels = [r for r in metadata.related.related_anime
               if r.relation_type == "sequel"]
    if not sequels: break
    next_node = sequels[0]
    if next_node.media_id in visited: break   # defend against MAL cycles
    visited.add(next_node.media_id)
    depth += 1

    tracked_status = lookup Anime for (user, next_node.media_id)

    if tracked_status in {In Progress, Completed}:
        break                                  # decision #9 stop
    if tracked_status == Dropped:
        current = next_node.media_id
        continue                               # decision #9 skip
    # else: not tracked OR Planning OR Paused → visible

    if visible_target is None:
        visible_target = next_node
        visible_depth = depth
    else:
        additional += 1
    current = next_node.media_id

if visible_target is None: return None
item = Item.get_or_create(media_id=visible_target.media_id, source="mal", media_type="anime", ...)
release_status = mal.anime(visible_target.media_id).details.status
return (item, additional, visible_depth, release_status)
```

Constants:

```python
SEQUEL_RELATION = "sequel"
CHAIN_STOP_STATUSES = {Status.IN_PROGRESS.value, Status.COMPLETED.value}
VISIBLE_TRACKED_STATUSES = {Status.PLANNING.value, Status.PAUSED.value}
MAX_DEPTH = 10
```

Helper `_get_tracked_status(user, media_id)` looks up the user's `Anime` row via `item__media_id` + `item__source="mal"` and returns its `status` or `None`.

## 8. Celery task

Added to `src/app/tasks.py`:

```python
@shared_task
def refresh_sequel_suggestions_for_user(user_id):
    user = User.objects.get(pk=user_id)
    kept_pks = set()  # pks of suggestions that were re-emitted this run

    completed = (
        Anime.objects
        .filter(user=user, status=Status.COMPLETED.value)
        .select_related("item")
    )
    for anime in completed:
        try:
            result = walk_sequel_chain(user, anime)
        except Exception:
            logger.exception("sequel chain walk failed: anime=%s", anime.pk)
            continue
        if result is None:
            continue
        target, additional, depth, release_status = result
        suggestion, _ = SequelSuggestion.objects.update_or_create(
            user=user, root=anime, target=target,
            defaults={
                "additional_count": additional,
                "chain_depth": depth,
                "target_release_status": release_status,
            },
        )
        kept_pks.add(suggestion.pk)

    # Garbage collect rows no longer valid (root left Completed,
    # target got tracked, chain shortened, etc.).
    SequelSuggestion.objects.filter(user=user).exclude(pk__in=kept_pks).delete()


@shared_task
def refresh_all_sequel_suggestions():
    for user_id in User.objects.values_list("pk", flat=True):
        refresh_sequel_suggestions_for_user.delay(user_id)
```

**Schedule:** daily 04:00 UTC via `django-celery-beat` `PeriodicTask`, following the existing pattern used for release fetching.

**GC semantics:**
- Unique constraint `(user, root, target)` → `update_or_create` uses the same row across runs.
- `dismissed_at` is only set in the user-facing endpoint, never by the task, so dismisses survive refreshes.
- Rows not re-emitted this run are deleted — that's how a suggestion disappears when the user changes a root away from `Completed` or adds the target with a hiding status.

**Manual fallback:** optional authenticated endpoint `POST /sequels/refresh` that enqueues `refresh_sequel_suggestions_for_user.delay(request.user.pk)` for debugging and first-use UX.

## 9. UI: home integration

### 9.1. View change

`src/app/views.py`, in `home(request)`, inside the loop that builds `home_sections`, insert the sequels section between the `In Progress` and `Planning` sections:

```python
if status == Status.IN_PROGRESS.value:
    sequels = (
        SequelSuggestion.objects
        .filter(user=request.user, dismissed_at__isnull=True)
        .select_related("target", "root__item")
        .order_by("-refreshed_at")[:items_limit]
    )
    if sequels.exists():
        home_sections.append({
            "key": "sequels_available",
            "id": "sequels-available",
            "label": "Sequels Available",
            "sequels": list(sequels),
            "count": sequels.count(),
            "is_sequel_section": True,
        })
```

Empty state = section omitted entirely (no placeholder).

### 9.2. Templates

`src/templates/app/components/home_section.html` — branch on `section.is_sequel_section`:

```django
{% if section.is_sequel_section %}
  {% include "app/components/home_sequel_section.html" with section=section %}
{% else %}
  {# existing rendering unchanged #}
{% endif %}
```

Two new templates:
- `app/components/home_sequel_section.html` — heading + grid wrapper.
- `app/components/sequel_card.html` — per-suggestion card.

Card contents:
- Target image + title.
- `target_release_status` badge (Airing / Finished / Upcoming).
- `Sequel to {{ root.item.title }}` label.
- `+{{ additional_count }} more` badge when `additional_count > 0`.
- Three action buttons:
  - **View details** — link to the existing `media_details` route for `(source=mal, media_type=anime, media_id=target.media_id)`.
  - **Add to Planning** — HTMX `POST /sequels/<id>/plan/`.
  - **Ignore** — HTMX `POST /sequels/<id>/dismiss/`.

### 9.3. New URLs and views

`src/app/urls.py` adds:

```
path("sequels/<int:pk>/plan/",    views.sequel_plan,    name="sequel_plan")
path("sequels/<int:pk>/dismiss/", views.sequel_dismiss, name="sequel_dismiss")
```

`views.sequel_plan(request, pk)`:
1. Fetch suggestion with `user=request.user` (404 otherwise).
2. Create an `Anime` row for `suggestion.target` with status `Planning` (follow existing form-based creation path — reuse the same validation/logic as the normal "add media" flow).
3. Delete the suggestion.
4. Return an HTMX partial that swaps the card out (via `hx-swap="outerHTML"` returning empty, or a toast partial).

`views.sequel_dismiss(request, pk)`:
1. Fetch suggestion with `user=request.user` (404 otherwise).
2. Set `dismissed_at = timezone.now()`; save.
3. Return an HTMX partial that removes the card.

Both endpoints use `@require_POST` and CSRF (Django default). Both are scoped by `user=request.user` to prevent cross-user access.

## 10. Signals — immediate invalidation

Added to `src/app/signals.py`:

```python
@receiver(post_save, sender=Anime)
def invalidate_sequel_suggestions_on_status_change(sender, instance, **kwargs):
    tracker = instance.tracker
    if "status" not in tracker.changed():
        return

    # Target was just tracked with a "hide" status → remove suggestions pointing at it.
    hide_statuses = {
        Status.IN_PROGRESS.value,
        Status.COMPLETED.value,
        Status.DROPPED.value,
    }
    if instance.status in hide_statuses:
        SequelSuggestion.objects.filter(
            user=instance.user,
            target=instance.item,
        ).delete()

    # Root left Completed → suggestions rooted here are no longer valid.
    if (
        tracker.previous("status") == Status.COMPLETED.value
        and instance.status != Status.COMPLETED.value
    ):
        SequelSuggestion.objects.filter(root=instance).delete()
```

- `FieldTracker` is already registered on `Anime` (`models.py:1845`); `tracker.changed()` / `tracker.previous("status")` both work.
- Deletion via `CASCADE` on the foreign keys handles the case when an `Anime` row is deleted.
- Creating new suggestions when a user *newly* enters `Completed` is intentionally **not** a signal — that stays with the daily task to avoid API calls during the request path.

## 11. Testing

New test modules under `src/app/tests/`, mirroring existing `app` test conventions:

1. `tests/test_sequels.py` — pure chain logic (`walk_sequel_chain`):
   - Simple chain: root Completed → S2 untracked → returns S2 with `additional=0`.
   - Long chain: root Completed → S2 untracked → S3 untracked → returns S2, `additional=1`.
   - Dedup (decision #7): S2 as `Planning` shows; `Completed` returns None.
   - Chain break (decision #9): S2 as `Dropped` → continue to S3; S2 as `In Progress` → return None.
   - Cycle defense: MAL returns S2 → S1 → S2 — no infinite loop, bounded by `visited` set and `MAX_DEPTH`.
   - `relation_type != "sequel"` entries (side_story, prequel) are ignored.
   - MAL responses mocked with `unittest.mock.patch` on `providers.mal.anime`.

2. `tests/test_sequel_tasks.py` — Celery task:
   - 3 Completed animes; mocked `walk_sequel_chain` returns for 2 — task creates exactly 2 `SequelSuggestion` rows.
   - Re-run with changed chain output — `update_or_create` updates the same row.
   - Garbage collect — stale rows from previous run are deleted.
   - `dismissed_at` survives re-runs.

3. `tests/test_sequel_views.py` — home + endpoints:
   - Home renders no section when no active suggestions exist.
   - Home renders cards in `-refreshed_at` order, limited by `items_limit`.
   - `POST /sequels/<id>/plan/` creates an `Anime` with status `Planning`, deletes the suggestion, returns 200.
   - `POST /sequels/<id>/dismiss/` sets `dismissed_at`, returns 200.
   - User A receives 404 when acting on User B's suggestion pk.

4. `tests/test_sequel_signals.py` — invalidation:
   - Root moves Completed → In Progress: rows `root=this` are deleted.
   - Target becomes In Progress / Completed / Dropped: rows `target=this.item` are deleted.
   - Target becomes Planning: rows are **not** deleted (decision #7 says Planning is visible).

Coverage target: the project standard of 80%+ on new modules (`app/sequels.py`, new task functions, new views, new signal handler).

## 12. Rollout

1. Migration is additive only — the new table + any indexes. No existing data altered. Zero-downtime.
2. On first deploy, the table is empty. First scheduled run of `refresh_all_sequel_suggestions` populates it.
3. Admins can trigger the refresh immediately via `python manage.py shell` → `refresh_all_sequel_suggestions.delay()` without waiting for beat.
4. If regressions appear, the feature can be disabled by un-scheduling the `PeriodicTask` and hiding the section via the `is_sequel_section` branch — no data migration required to roll back.

## 13. Open items / future iterations (out of scope for v1)

- Manga sequels (re-use the same model + task, swap provider/relation field).
- Apprise push notifications for newly-detected upcoming sequels.
- "Un-ignore" UI (admin or user-facing).
- Richer relation handling (side_story, parent_story) behind a user preference.
- Cover the case where a user's root anime has *multiple* direct sequels (unusual but possible in MAL's data) — v1 picks the first; could be expanded later.
