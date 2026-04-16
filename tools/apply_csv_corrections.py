"""Apply user-confirmed corrections to the migration CSV."""
import csv
import sys
from pathlib import Path

CSV_PATH = sys.argv[1] if len(sys.argv) > 1 else "/opt/services/apps/yamtrack-src/migration_mapping.csv"

# Rules: anime_media_id -> {field: value}
# - "action=skip" means don't migrate this anime
# - "action=apply" means trust the row (migrate as-is)
# - tmdb_id/season_number overrides are explicit

OVERRIDES = {
    # Movies (not TV shows) — skip
    "16782": {"action": "skip"},   # Kotonoha no Niwa (The Garden of Words)
    "199":   {"action": "skip"},   # Sen to Chihiro (Spirited Away)

    # HxH 1999 original (MAL 136) was wrongly mapped to 2011 TMDB entry
    "136":   {"tmdb_id": "45952", "tmdb_title": "Hunter x Hunter", "action": "apply"},

    # HxH specials/arcs — no clean TMDB parallel, skip
    "137":   {"action": "skip"},   # HxH OVA
    "138":   {"action": "skip"},   # HxH Greed Island
    "139":   {"action": "skip"},   # HxH Greed Island Final

    # Code Geass R2 → season 2
    "2904":  {"season_number": "2", "action": "apply"},

    # Tokyo Ghoul √A → season 2
    "27899": {"season_number": "2", "action": "apply"},

    # Zero no Tsukaima variants
    "11319": {"tmdb_id": "35753", "tmdb_title": "The Familiar of Zero", "season_number": "4", "action": "apply"},
    "1840":  {"tmdb_id": "35753", "tmdb_title": "The Familiar of Zero", "season_number": "2", "action": "apply"},
    "3712":  {"tmdb_id": "35753", "tmdb_title": "The Familiar of Zero", "season_number": "3", "action": "apply"},

    # Noragami Aragoto → season 2
    "30503": {"season_number": "2", "action": "apply"},

    # Spy x Family Part 2 → season 1 (MAL split cour, TMDB merges as single S1)
    "50602": {"season_number": "1", "action": "apply"},

    # Danna ga Nani 2 Sure-me → season 2
    "29067": {"season_number": "2", "action": "apply"},

    # One Piece: use classic anime ID (37854) not Netflix live-action (111110)
    "21":    {"tmdb_id": "37854", "tmdb_title": "One Piece", "action": "apply"},

    # Mob Psycho 100: all three MAL entries → TMDB 67075 (which has seasons 1-3)
    "32182": {"tmdb_id": "67075", "tmdb_title": "Mob Psycho 100", "season_number": "1", "action": "apply"},
    "37510": {"tmdb_id": "67075", "tmdb_title": "Mob Psycho 100", "season_number": "2", "action": "apply"},
    "50172": {"tmdb_id": "67075", "tmdb_title": "Mob Psycho 100", "season_number": "3", "action": "apply"},

    # TMDB Season-2 gaps — shows where the S2 isn't indexed. Skip; user can
    # cadastrar as a separate TV manually later, or the command can be
    # re-run after TMDB adds the data.
    "51009": {"action": "skip"},  # Jujutsu Kaisen 2nd Season
    "31637": {"action": "skip"},  # Gate Part 2
    "58567": {"action": "skip"},  # Solo Leveling S2
    "60564": {"action": "skip"},  # Ranma 2024 S2
    "60285": {"action": "skip"},  # Sakamoto Days Part 2
}


def apply_corrections(csv_path):
    path = Path(csv_path)
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    for row in rows:
        mid = row["anime_media_id"]
        if mid in OVERRIDES:
            row.update(OVERRIDES[mid])
            continue
        # For any other "manual" row with a non-empty tmdb_id, trust top match
        # (user already confirmed translations are fine).
        if row.get("action") == "manual":
            if row.get("tmdb_id", "").strip():
                row["action"] = "apply"
            else:
                row["action"] = "skip"

    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    counts = {"apply": 0, "skip": 0, "manual": 0}
    for row in rows:
        counts[row.get("action", "")] = counts.get(row.get("action", ""), 0) + 1
    print(f"After corrections: {counts}")


if __name__ == "__main__":
    apply_corrections(CSV_PATH)
