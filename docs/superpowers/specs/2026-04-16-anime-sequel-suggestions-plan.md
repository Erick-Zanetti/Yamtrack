# Implementation Plan: Anime Sequel Suggestions

Companion to `2026-04-16-anime-sequel-suggestions-design.md`. The spec is the source of truth for behavior; this plan is the execution ordering.

## Overview

Implement the anime sequel suggestions feature per the approved design. Anchors verified in the codebase:

- `src/app/models.py`: `Sources` (L38), `MediaTypes` (L52), `Item` (L67), `Status` (L765), `Media` (L818), `Anime` (L1842) with `tracker = FieldTracker()` (L1845).
- `src/app/views.py`: `home()` at L41 builds `home_sections` inside a `for status in (IN_PROGRESS, PLANNING)` loop (L63-L80); `media_save()` at L531 shows the Item+Form creation pattern to reuse.
- `src/app/providers/mal.py`: `anime()` at L93 with `cache.set(cache_key, data)` (L153); `get_related()` at L384 currently drops `relation_type`; `get_readable_status()` at L240 produces the badge strings ("Airing"/"Finished"/"Upcoming").
- `src/app/apps.py` already imports `app.signals` in `ready()` (L12), so new signal handlers only need to live in `src/app/signals.py`.
- `src/app/tasks.py` already uses `@shared_task`.
- `src/app/urls.py` is a flat list of `path(...)` entries.
- `src/integrations/imports/helpers.py` L223-L246 is the canonical example for creating `CrontabSchedule` + `PeriodicTask` in a data migration.
- Test conventions: `src/app/tests/test_tasks.py` (Celery), `src/app/tests/views/test_home.py` (views + mocked metadata), `src/app/tests/models/` (models). Mocks target `app.providers.services.get_media_metadata` or `app.providers.mal.anime`.

---

## Phase 0 — Pre-flight verifications (no code)

Before Phase 1, confirm:

1. **`FieldTracker.previous()` availability.** Design references `tracker.previous("status")`. Confirm against the installed version of `django-model-utils`. Fallback: `tracker.changed().get("status")` returns the pre-save value.
2. **Historical records cascade.** `Anime` inherits `HistoricalRecords` via `Media`. Confirm adding `SequelSuggestion` with `ForeignKey(Anime)` does not require `excluded_fields` changes.
3. **Celery-beat scheduler.** Verify `CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"` in settings so a `PeriodicTask` row actually runs.
4. **Cache backend in tests.** MAL provider uses `django.core.cache.cache.set`. Ensure test settings use `LocMemCache` or that tests clear cache in `setUp`.
5. **`AUTH_USER_MODEL`.** Use `settings.AUTH_USER_MODEL` (string) in FKs, not concrete class.
6. **`AppConfig` label.** App name is `app`; no label override needed.

---

## Phase 1 — MAL provider change (smallest independent slice)

Goal: expose `relation_type` in `get_related()`. Additive; safe to ship alone.

### Step 1.1 — Test: `relation_type` preserved by `get_related()`
- Files: `src/app/tests/providers/test_metadata.py`.
- Action: test that `mal.get_related([...payload with relation_type "sequel"...], MediaTypes.ANIME.value)` includes `"relation_type": "sequel"`. Test with missing relation_type → key present with `None`.

### Step 1.2 — Implementation: preserve `relation_type` in `get_related()`
- Files: `src/app/providers/mal.py` (L384-L397).
- Action: add `"relation_type": media.get("relation_type")` to the dict comprehension.

### Step 1.3 — Guard test: `anime()` metadata exposes `relation_type`
- Files: `src/app/tests/providers/test_metadata.py`.
- Action: patch `services.api_request` with a fixture containing a sequel; assert `data["related"]["related_anime"][0]["relation_type"] == "sequel"`. Clear cache in `setUp`.

**End of Phase 1:** Tests green, no user-visible change.

---

## Phase 2 — Core chain-walking module (pure logic)

Goal: `app.sequels.walk_sequel_chain` against mocked MAL responses. No DB schema yet. Module is unused at end of phase.

### Step 2.1 — Test: simple one-hop chain
- Files: `src/app/tests/test_sequels.py` (new).
- Action: user + `Item` + `Anime(status=Completed)` for root (MAL id "1"). Mock `app.providers.mal.anime` for id 2 returns a sequel of nothing; id 1 returns sequel to id 2. Assert `walk_sequel_chain(user, root)` returns `(item, 0, 1, "Finished")` and Item was created via `get_or_create`.

### Step 2.2 — Test: multi-hop yields `+N more`
- Files: same.
- Action: root→S2→S3 (neither tracked). Assert `additional_count == 1`, `chain_depth == 1`, target=S2.

### Step 2.3 — Test: dedup rules (decision #7)
- Files: same.
- Action: subtests for {not tracked, Planning, Paused} → S2 visible; {In Progress, Completed} → None; Dropped → skip to S3.

### Step 2.4 — Test: chain-break walks through Dropped (decision #9)
- Files: same.
- Action: root→S2(Dropped)→S3(untracked)→nothing. Assert target=S3, `additional=0`, `depth=2`.

### Step 2.5 — Test: cycle defense
- Files: same.
- Action: id 1→2, id 2→1. Assert terminates; returns S2.

### Step 2.6 — Test: `MAX_DEPTH` respected
- Files: same.
- Action: infinite forward-only unique chain. Assert `mal.anime` called ≤ `MAX_DEPTH + 1` times.

### Step 2.7 — Test: non-sequel relations ignored
- Files: same.
- Action: only side_story/prequel/recap relations. Assert returns `None` and no Item created.

### Step 2.8 — Implementation: `src/app/sequels.py`
- Files: `src/app/sequels.py` (new).
- Action: implement constants (`SEQUEL_RELATION`, `CHAIN_STOP_STATUSES`, `VISIBLE_TRACKED_STATUSES`, `MAX_DEPTH=10`), helper `_get_tracked_status(user, media_id)` (cast `media_id` via `str(...)` — `Item.media_id` is `CharField`), and `walk_sequel_chain(user, root_anime)` per spec §7.2. Returns tuple `(item, additional_count, chain_depth, release_status)`.

### Step 2.9 — Test: error contract
- Files: same.
- Action: patch `mal.anime` to raise; assert the function re-raises (caller is responsible for logging+skipping per spec §8). Document contract in module docstring.

**End of Phase 2:** Module tested but unused.

---

## Phase 3 — Data model + migration

### Step 3.1 — Test: model constraints
- Files: `src/app/tests/models/test_sequel_suggestion.py` (new).
- Action: unique on `(user, root, target)`; defaults for `additional_count`, `chain_depth`, `dismissed_at`; cascade on `user`/`root`/`target` delete; ordering `-refreshed_at`.

### Step 3.2 — Implementation: add `SequelSuggestion` to `src/app/models.py`
- Files: `src/app/models.py`.
- Action: class per spec §6. Use `settings.AUTH_USER_MODEL` and string FK references `"Anime"`, `"Item"`.

### Step 3.3 — Generate migration
- Files: auto-generated, e.g. `src/app/migrations/00XX_sequelsuggestion.py` (XX = current max + 1; current max verified at 0060).
- Action: `python manage.py makemigrations app`. Inspect `sqlmigrate` on SQLite and Postgres.

### Step 3.4 — Smoke: Django runs migrations before tests
- No extra test needed; Step 3.1 exercises the schema.

**End of Phase 3:** Table exists.

---

## Phase 4 — Celery task + periodic schedule

### Step 4.1 — Test: task creates rows for Completed animes
- Files: `src/app/tests/test_sequel_tasks.py` (new).
- Action: 3 Completed for user_a, 1 for user_b. Mock `walk_sequel_chain` to return for 2 of user_a's roots, None for third. Call task with `user_a.pk`. Assert 2 rows for user_a, 0 for user_b.

### Step 4.2 — Test: re-run updates in place
- Files: same.
- Action: call task, assert row with `additional_count=0`. Change mock to `additional=2, depth=3, status="Airing"`. Re-run. Assert 1 row, fields updated, `refreshed_at` advanced.

### Step 4.3 — Test: GC deletes stale rows
- Files: same.
- Action: seed a row whose root is no longer Completed. Run task. Assert row deleted.

### Step 4.4 — Test: `dismissed_at` survives refresh
- Files: same.
- Action: seed row with `dismissed_at=now`, valid root, target matching mocked walk. Run task. Assert `dismissed_at` still set (defaults dict must NOT include it).

### Step 4.5 — Test: chain walk exception logged+skipped
- Files: same.
- Action: walk raises on first anime, succeeds on second. Assert 1 row created, exception logged.

### Step 4.6 — Test: `refresh_all_sequel_suggestions` fans out
- Files: same.
- Action: patch `refresh_sequel_suggestions_for_user.delay`; assert called once per user.

### Step 4.7 — Implementation: tasks in `src/app/tasks.py`
- Files: `src/app/tasks.py`.
- Action: per spec §8. Use `select_related("item")` on Completed queryset. GC uses accumulated `kept_pks`. Task names: `"Refresh sequel suggestions for user"` and `"Refresh all sequel suggestions"`.

### Step 4.8 — Test: data migration creates `PeriodicTask`
- Files: `src/app/tests/test_sequel_tasks.py`.
- Action: assert `PeriodicTask.objects.filter(task="Refresh all sequel suggestions").exists()`; crontab hour=4, minute=0, enabled=True.

### Step 4.9 — Implementation: data migration
- Files: `src/app/migrations/00YY_schedule_sequel_refresh.py` (YY = Phase 3 + 1).
- Action: `migrations.RunPython` with forward creating `CrontabSchedule` (hour=4, minute=0, UTC) and `PeriodicTask`, and reverse deleting the `PeriodicTask` by name. Pattern: copy from `src/integrations/imports/helpers.py` L223-L246. Check `django_celery_beat` dep version via `pip show django-celery-beat` before pinning the migration dependency.

**End of Phase 4:** Rows populate via cron. Still invisible.

---

## Phase 5 — Signals for immediate invalidation

### Step 5.1 — Test: target enters hide-status → suggestion deleted
- Files: `src/app/tests/test_sequel_signals.py` (new).
- Action: seed `SequelSuggestion(target=item)`. Create `Anime(item=item, status=IN_PROGRESS)`. Assert suggestion deleted. Repeat for COMPLETED and DROPPED.

### Step 5.2 — Test: target enters Planning → suggestion kept
- Files: same.
- Action: seed; create `Anime(..., status=Planning)`. Assert still exists.

### Step 5.3 — Test: root leaves Completed → suggestions rooted there deleted
- Files: same.
- Action: seed `Anime(root, status=COMPLETED)` + suggestion. Update root to IN_PROGRESS. Assert `filter(root=root).exists()` false.

### Step 5.4 — Test: status unchanged → no deletions
- Files: same.
- Action: save without status change. Assert suggestions intact.

### Step 5.5 — Test: root deleted → cascade (no signal)
- Files: same.
- Confirms cascade path, not the signal.

### Step 5.6 — Implementation: `src/app/signals.py`
- Files: `src/app/signals.py`.
- Action: handler per spec §10. Early-exit when `"status" not in instance.tracker.changed()`. No creation logic here.

**End of Phase 5:** Rows accurate within-request.

---

## Phase 6 — Plan / Dismiss endpoints

### Step 6.1 — Test: `sequel_plan` creates Planning Anime + deletes suggestion
- Files: `src/app/tests/test_sequel_views.py` (new).
- Action: seed suggestion. POST `/sequels/<pk>/plan/`. Assert 200/204, `Anime.objects.filter(user, item=target, status=Planning).exists()`, suggestion deleted, Item not duplicated.

### Step 6.2 — Test: `sequel_plan` returns HTMX partial
- Files: same. Pin the exact template used.

### Step 6.3 — Test: cross-user 404
- Files: same.

### Step 6.4 — Test: requires POST
- Files: same. GET → 405.

### Step 6.5 — Test: `sequel_dismiss` sets `dismissed_at`
- Files: same. Row still exists.

### Step 6.6 — Test: dismiss cross-user 404
- Files: same.

### Step 6.7 — Test: dismiss requires POST
- Files: same.

### Step 6.8 — Implementation: `sequel_plan` + `sequel_dismiss` in `src/app/views.py`
- Files: `src/app/views.py`.
- Action:
  - `get_object_or_404(SequelSuggestion, pk=pk, user=request.user)`.
  - `sequel_plan`: **recommended** direct `Anime.objects.create(item=suggestion.target, user=request.user, status=Status.PLANNING.value)` — no user input to validate. Document this choice in a code comment. Then `suggestion.delete()`.
  - `sequel_dismiss`: set `dismissed_at = timezone.now()`, `save(update_fields=["dismissed_at"])`.
  - Return `HttpResponse(status=204)` or render a minimal partial.
- Both `@require_POST`.

### Step 6.9 — URL wiring
- Files: `src/app/urls.py`.
- Action: two `path` entries per spec §9.3, names `sequel_plan`, `sequel_dismiss`.

**End of Phase 6:** Endpoints live and tested.

---

## Phase 7 — Home rendering

### Step 7.1 — Test: home renders section with active suggestions
- Files: `src/app/tests/views/test_home.py` (extend).
- Action: seed 2 active + 1 dismissed. GET `/`. Assert heading "Sequels Available" present, both card titles present, dismissed title absent, ordering In Progress → Sequels → Planning.

### Step 7.2 — Test: home omits section when no active suggestions
- Files: same.
- Action: no rows / all dismissed. Assert heading absent.

### Step 7.3 — Test: limit + ordering
- Files: same.
- Action: 20 rows staggered `refreshed_at`. Assert 14 cards in descending `refreshed_at`.

### Step 7.4 — Test: card content
- Files: same.
- Action: row with `additional_count=3`, `release_status="Airing"`. Assert "+3 more", "Airing", and URLs for `sequel_plan`, `sequel_dismiss`, `media_details`.

### Step 7.5 — Implementation: home view
- Files: `src/app/views.py`.
- Action: per spec §9.1 — after In Progress section, insert sequels section (only if non-empty). `.select_related("target", "root__item").order_by("-refreshed_at")[:items_limit]`. Set `is_sequel_section=True`.

### Step 7.6 — Implementation: template branching
- Files: `src/templates/app/components/home_section.html`.
- Action: wrap body in `{% if section.is_sequel_section %}…{% else %}…{% endif %}`.

### Step 7.7 — Implementation: `home_sequel_section.html`
- Files: `src/templates/app/components/home_sequel_section.html` (new).
- Heading + grid loop including `sequel_card.html`.

### Step 7.8 — Implementation: `sequel_card.html`
- Files: `src/templates/app/components/sequel_card.html` (new).
- Target image/title, "Sequel to {{ s.root.item.title }}", release-status badge, conditional `+N more`, three action buttons:
  - View details → `{% url 'media_details' source=s.target.source media_type=s.target.media_type media_id=s.target.media_id title=s.target.title|slugify %}`.
  - Add to Planning → `hx-post="{% url 'sequel_plan' s.pk %}"` with `hx-target` on card wrapper.
  - Ignore → `hx-post="{% url 'sequel_dismiss' s.pk %}"`.
- Match CSRF/HTMX pattern used by `progress_edit` in the existing templates — grep before inventing.

### Step 7.9 — Optional: removal partial
- Files: `src/templates/app/components/sequel_card_removed.html`.
- Action: empty/toast partial for swap targets; refactor Phase 6 views to render it if preferred over 204.

### Step 7.10 — Manual sanity
- Run server, create a Completed anime with known sequel (e.g. *Aldnoah.Zero* MAL id `21603` → sequel `23289`), trigger `refresh_sequel_suggestions_for_user.delay(request.user.pk)` from `manage.py shell`, reload home, verify card + both actions.

**End of Phase 7:** Feature user-visible.

---

## Phase 8 (optional) — Manual refresh endpoint

Deferred. Only if onboarding UX needs it before first daily cron.

### Step 8.1 — Test + endpoint
- POST `/sequels/refresh` → `refresh_sequel_suggestions_for_user.delay(request.user.pk)` → 202. `@require_POST`, authenticated.

---

## Testing Strategy Summary

| Layer | File | Target |
|-------|------|--------|
| Unit — chain logic | `src/app/tests/test_sequels.py` | 95%+ on `app/sequels.py` |
| Unit — model | `src/app/tests/models/test_sequel_suggestion.py` | constraints/cascade exhaustive |
| Integration — tasks | `src/app/tests/test_sequel_tasks.py` | both tasks + GC + dismiss persistence |
| Integration — signals | `src/app/tests/test_sequel_signals.py` | all three decision rules |
| Integration — views | `src/app/tests/test_sequel_views.py` | cross-user + HTMX shape |
| View — home | `src/app/tests/views/test_home.py` additions | order, limit, empty |
| Provider | `src/app/tests/providers/test_metadata.py` | `relation_type` passthrough |

All MAL calls mocked at `app.providers.mal.anime` (not at `api_request`). Clear `django.core.cache.cache` in `setUp` on cache-touching tests.

---

## Risks & Open Questions

1. **`FieldTracker.previous()` availability** (Phase 5). Confirm before Phase 5. Fallback: `tracker.changed().get("status")`.
2. **Form vs direct create in `sequel_plan`** (Phase 6). Recommend direct `Anime.objects.create(...)` (status hardcoded to Planning, no user input to validate).
3. **`chain_depth` semantics** (Phase 2). Tests assume `visible_depth` = depth at which visible target was picked, not full chain length.
4. **`target_release_status` strings** (Phase 2). `mal.get_readable_status` yields "Finished"/"Airing"/"Upcoming"/etc. Confirm renderable as-is or map.
5. **Duplicate MAL lookups** (Phase 2). Visible target may be fetched twice (walk + release_status). Acceptable under 24h cache.
6. **SQLite vs Postgres migration parity** (Phase 3). Run `sqlmigrate` on both before merging.
7. **`django_celery_beat` migration dep** (Phase 4.9). Pin to the version in `requirements.txt`; do not hardcode `0019` without confirming.
8. **MAL `relation_type` capitalization** (Phase 1). Should be lowercase "sequel"; confirm with fixture.
9. **Multiple direct sequels** (spec §13). V1 picks first; log debug when `len(sequels) > 1`.
10. **Signal + simple_history ordering** (Phase 5). Our handler deletes unrelated rows; should not race, but verify history intact in a signal test.
11. **User fan-out performance** (Phase 4.7). Sequential enqueue. Fine at current scale; revisit if user count grows.
12. **CSRF on HTMX POSTs** (Phase 7.8). Replicate the pattern used by `progress_edit`; don't invent a new one.

---

## Success Criteria

- [ ] Phase 1: `mal.get_related` preserves `relation_type`; existing tests still green.
- [ ] Phase 2: `walk_sequel_chain` passes all unit tests; no user-visible change.
- [ ] Phase 3: migration applies cleanly on SQLite and Postgres; model tests pass.
- [ ] Phase 4: `PeriodicTask` row exists, enabled, daily 04:00 UTC; tasks update/delete correctly; `dismissed_at` preserved.
- [ ] Phase 5: signals delete rows on hide-status transitions; Planning transition non-destructive.
- [ ] Phase 6: both endpoints scoped by user, POST-only, 404 cross-user.
- [ ] Phase 7: home renders section between In Progress and Planning; correct cards, actions, order, limit; omits cleanly when empty.
- [ ] 80%+ coverage on new modules.

---

## File Index (absolute)

**Created:**
- `src/app/sequels.py`
- `src/app/migrations/00XX_sequelsuggestion.py` (auto)
- `src/app/migrations/00YY_schedule_sequel_refresh.py`
- `src/app/tests/test_sequels.py`
- `src/app/tests/test_sequel_tasks.py`
- `src/app/tests/test_sequel_views.py`
- `src/app/tests/test_sequel_signals.py`
- `src/app/tests/models/test_sequel_suggestion.py`
- `src/templates/app/components/home_sequel_section.html`
- `src/templates/app/components/sequel_card.html`
- `src/templates/app/components/sequel_card_removed.html` (optional)

**Modified:**
- `src/app/providers/mal.py`
- `src/app/models.py`
- `src/app/tasks.py`
- `src/app/signals.py`
- `src/app/views.py`
- `src/app/urls.py`
- `src/templates/app/components/home_section.html`
- `src/app/tests/providers/test_metadata.py`
- `src/app/tests/views/test_home.py`
