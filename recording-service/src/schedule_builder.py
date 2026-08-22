import uuid
from typing import Any

import pendulum

from src import utils
from src.config import _parse_start_time_and_duration  # type: ignore
from src.models import RecordingSchedule
from src import settings


def build_schedule(row: dict[str, Any]) -> RecordingSchedule:
    """Build a domain RecordingSchedule from a DB row (joined with its station)."""
    user_tz = pendulum.timezone(settings.TIME_ZONE)

    start_utc, duration = _parse_start_time_and_duration(
        row["start_time"], row["end_time"], user_tz
    )

    audio_format = row.get("audio_format", "mp3")
    frequency = row.get("frequency", "*")

    schedule = RecordingSchedule(
        title=row["title"],
        station_name=row["station_name"],
        station_url=row["station_url"],
        start_timeofday=start_utc,
        duration=duration,
        audio_format=audio_format,
        output_dir=settings.OUTPUT_DIR,
        metadata={
            "title": row["title"],
            "station": row["station_name"],
            "station_url": row["station_url"],
        },
        frequency=frequency,
    )

    # Align the schedule id with the DB id so the scheduler job can be
    # removed/re-added on update/delete.
    object.__setattr__(schedule, "id", uuid.UUID(row["id"]))
    return schedule
