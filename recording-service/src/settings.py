import os
from pathlib import Path

# Global settings, overridable via environment variables.
# A single personal instance uses one timezone (the user's local zone).
TIME_ZONE = os.getenv("RADIOTIMER_TZ", "Europe/Berlin")

# Base directory where recordings are stored.
OUTPUT_DIR = Path(os.getenv("RADIOTIMER_OUTPUT", "./recordings"))

# Global path pattern for recordings, relative to OUTPUT_DIR.
# Mirrors the old "VLC Timer" naming: <station>/<title>/<YYYY>-<MM>-<DD> <HH>-<MM>.mp3
# Available placeholders: {station}, {title}, {date}, {start}, {end},
# {start_hm}, {end_hm}, {ext}, {id}
PATTERN = os.getenv(
    "RADIOTIMER_PATTERN", "{station}/{title}/{date} {start_hm}.{ext}"
)

# SQLite database file storing the schedules.
DB_PATH = Path(os.getenv("RADIOTIMER_DB", "./app.db"))
