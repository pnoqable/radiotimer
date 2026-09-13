import uuid
from typing import Any, Optional

import pendulum
from pendulum import Time  # type: ignore
from pendulum.parser import parse as pendulum_parse

from src import utils
from src.config import _parse_start_time_and_duration  # type: ignore
from src.models import RecordingSchedule
from src import settings


def _parse_local_start_time(start_time: str) -> Optional[Time]:
    # The UI stores the local wall-clock start as "HH:MM". Fall back to None if
    # it cannot be parsed, so one-off windows keep the legacy UTC behaviour.
    try:
        parsed = pendulum_parse(start_time, exact=True)
        return parsed if isinstance(parsed, Time) else None
    except Exception:
        return None


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
        one_off=row.get("one_off", False),
        start_date=row.get("start_date"),
        start_time_local=_parse_local_start_time(row["start_time"]),
    )

    # Align the schedule id with the DB id so the scheduler job can be
    # removed/re-added on update/delete.
    object.__setattr__(schedule, "id", uuid.UUID(row["id"]))
    return schedule
