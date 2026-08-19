import uuid
from typing import Any

import pendulum
from slugify import slugify

from src import utils
from src.config import _parse_start_time_and_duration  # type: ignore
from src.models import RecordingSchedule, ValidUrl
from src import settings


def build_schedule(row: dict[str, Any]) -> RecordingSchedule:
    """Build a domain RecordingSchedule from a DB row."""
    user_tz = pendulum.timezone(settings.TIME_ZONE)

    start_utc, duration = _parse_start_time_and_duration(
        row["start_time"], row["end_time"], user_tz
    )

    subdir = row.get("subdir") or slugify(row["title"])
    schedule_dir = settings.OUTPUT_DIR / subdir

    audio_format = row.get("audio_format", "mp3")
    frequency = row.get("frequency", "*")

    schedule = RecordingSchedule(
        title=row["title"],
        start_timeofday=start_utc,
        duration=duration,
        audio_format=audio_format,
        output_dir=schedule_dir,
        metadata={
            "title": row["title"],
            "stream_url": row["stream_url"],
            "description": row.get("description"),
        },
        description=row.get("description"),
        frequency=frequency,
        stream_url=ValidUrl(row["stream_url"]),
    )

    # Align the schedule id with the DB id so the scheduler job can be
    # removed/re-added on update/delete.
    object.__setattr__(schedule, "id", uuid.UUID(row["id"]))
    return schedule
