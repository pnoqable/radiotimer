import os
from pathlib import Path

# Global settings, overridable via environment variables.
# A single personal instance uses one timezone (the user's local zone).
TIME_ZONE = os.getenv("RADIOTIMER_TZ", "Europe/Berlin")

# Base directory where recordings are stored (one subdir per schedule).
OUTPUT_DIR = Path(os.getenv("RADIOTIMER_OUTPUT", "./recordings"))

# SQLite database file storing the schedules.
DB_PATH = Path(os.getenv("RADIOTIMER_DB", "./app.db"))
