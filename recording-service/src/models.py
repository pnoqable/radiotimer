import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import validators  # type: ignore
from croniter import croniter
from pendulum import Date, DateTime, Duration, Period, Time  # type: ignore
from typing_extensions import override

from src import settings, utils
from src.utils import TimePeriod

logger = logging.getLogger(__name__)


class ValidUrl(str):
    @override
    def __new__(cls, value: str):
        if not validators.url(value):  # type: ignore
            raise ValueError(f"Invalid url: {value}")
        return super().__new__(cls, value)


@dataclass(frozen=True)
class RecordingTask:
    title: str
    station: str
    recording_period: TimePeriod
    base_dir: Path
    audio_format: str
    stream_url: ValidUrl
    pattern: str = settings.PATTERN
    actual_start: Optional[DateTime] = None
    id: uuid.UUID = field(default_factory=lambda: uuid.uuid4())

    def __post_init__(self):
        file_path = self._make_file_path()
        object.__setattr__(self, "file_path", file_path)  # Mutates frozen object

    # Build the output path from the global pattern.
    def _make_file_path(self) -> Path:
        # The timestamp reflects when the recording actually starts (set by the
        # scheduler), not the schedule's defined start time. The defined start
        # is only used as a fallback when no actual start is known (e.g. when a
        # task is built directly in tests).
        start = self.actual_start if self.actual_start is not None else self.recording_period.start
        end = self.recording_period.end
        rel = self.pattern.format(
            station=_safe_name(self.station),
            title=_safe_name(self.title),
            date=start.strftime("%Y-%m-%d"),
            start=start.strftime("%H%M"),
            start_hm=start.strftime("%H-%M"),
            end=end.strftime("%H%M"),
            end_hm=end.strftime("%H-%M"),
            ext=self.audio_format,
            id=str(self.id),
        )
        return self.base_dir / rel


def _safe_name(name: str) -> str:
    """Make a string safe to use as a path component while keeping the
    original casing, spaces and umlauts (only strip path separators and
    control characters)."""
    name = name.strip()
    name = re.sub(r"[\x00-\x1f/\\]", "_", name)
    name = name.strip().strip(".")
    return name or "unnamed"


@dataclass(frozen=True)
class RecordingSchedule:
    """A recording schedule references a station and defines a daily recording
    period. If the start time is later than the end time, the recording is
    assumed to span across midnight.

    Args:
        title (str): The title of the schedule.
        station_name (str): Name of the station (used for the output path).
        station_url (str): The station's stream/playlist URL (resolved at record time).
        start_timeofday (Time): Start time of day for the recording period in UTC.
        output_dir (Path): The base output directory for recordings.
    """

    title: str
    station_name: str
    station_url: str
    start_timeofday: Time
    duration: Duration
    audio_format: str
    output_dir: Path
    metadata: dict[str, Any] = field(default_factory=dict)

    id: uuid.UUID = field(init=False, default_factory=lambda: uuid.uuid4())

    # Optional
    frequency: str = "*"  # Defaults to "daily" cron expression

    @property
    def end_timeofday(self) -> Time:
        return self.start_timeofday.add(seconds=self.duration.in_seconds())

    # Converts the schedule frequency to a cron expression
    @property
    def cron_expression(self) -> str:
        return f"{self.start_timeofday.minute} {self.start_timeofday.hour} * * {self.frequency}"

    def __post_init__(
        self,
    ):
        if not len(self.title.strip()):
            raise ValueError("Title cannot be empty")

        # Remove any spaces to adhere to cron expression format
        object.__setattr__(
            self, "frequency", self.frequency.replace(" ", "")
        )  # Mutates frozen object

    # Gets the current or next task. The station's playlist URL is resolved
    # to a recordable stream URL right before the recording starts.
    async def get_current_or_next_task(self, recording_start_time: DateTime) -> RecordingTask:
        from src.playlist import resolve_stream_url

        recording_period = self.resolve_recording_period(recording_start_time)
        resolved = await resolve_stream_url(self.station_url)

        return RecordingTask(
            title=self.title,
            station=self.station_name,
            recording_period=recording_period,
            base_dir=self.output_dir,
            audio_format=self.audio_format,
            stream_url=ValidUrl(resolved),
            actual_start=recording_start_time,
        )

    def resolve_recording_period(self, recording_start_time: DateTime) -> TimePeriod:
        # Get prev recording period based on cron expression (as we may be within the prev recording period)
        prev_start_time: DateTime = croniter(
            self.cron_expression, start_time=recording_start_time
        ).get_prev(datetime)

        prev_end_time: DateTime = prev_start_time + self.duration.as_timedelta()

        # Check if we are still within the prev recording period
        if recording_start_time < prev_end_time:
            # If recording has been started before the end of the previous recording period, we are still within the previous recording period
            logger.debug(
                f"Schedule '{self.title}': Recording has been started during previous recording period"
            )
            return TimePeriod(
                start=prev_start_time,
                end=prev_end_time,
            )
        else:
            # Get next recording period based on cron expression
            next_start_time: DateTime = croniter(
                self.cron_expression, start_time=recording_start_time
            ).get_next(datetime)
            next_end_time: DateTime = next_start_time + self.duration.as_timedelta()

            return TimePeriod(
                start=next_start_time,
                end=next_end_time,
            )
