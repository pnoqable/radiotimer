import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import validators  # type: ignore
from croniter import croniter
import pendulum  # type: ignore
from pendulum import Date, DateTime, Duration, Interval, Time  # type: ignore
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
        tz = pendulum.timezone(settings.TIME_ZONE)
        start = _to_local(
            self.actual_start if self.actual_start is not None else self.recording_period.start,
            tz,
        )
        end = _to_local(self.recording_period.end, tz)
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


def _to_local(dt, tz):
    """Return ``dt`` converted to ``tz``.

    Internal recording times are UTC (and may be naive datetimes that really
    represent UTC). Treat naive datetimes as UTC before converting, so the
    file-name timestamp is shown in the user's local timezone.
    """
    if dt.tzinfo is None:
        return pendulum.instance(dt, tz="UTC").in_tz(tz)
    return dt.in_tz(tz)


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

    # One-off recordings: schedule fires exactly once on `start_date`
    # (YYYY-MM-DD, local calendar date) at the schedule's start time.
    one_off: bool = False
    start_date: Optional[str] = None

    # Local wall-clock start time as entered in the UI (HH:MM). Used to
    # reconstruct the absolute one-off start on `start_date`: the stored UTC
    # `start_timeofday` alone is ambiguous across the UTC/local midnight
    # boundary (e.g. 01:30 Berlin = 23:30 UTC on the previous day).
    start_time_local: Optional[Time] = None

    @property
    def end_timeofday(self) -> Time:
        return self.start_timeofday.add(seconds=self.duration.in_seconds())

    # Wall-clock start time and timezone to feed the recurring cron trigger and
    # the croniter window matching. Schedules built from the DB carry the local
    # wall-clock start time (start_time_local); their cron fires at that LOCAL
    # time, so DST shifts and the UTC/local midnight boundary (e.g. Monday
    # 01:30 Berlin = Sunday 23:30 UTC) are handled by the timezone. Schedules
    # without it (legacy / directly constructed) keep the UTC wall-clock time.
    def cron_start(self) -> tuple[Time, str]:
        if self.start_time_local is not None:
            return self.start_time_local, settings.TIME_ZONE
        return self.start_timeofday, "UTC"

    # Converts the schedule frequency to a cron expression
    @property
    def cron_expression(self) -> str:
        start, _ = self.cron_start()
        return f"{start.minute} {start.hour} * * {self.frequency}"

    # Absolute UTC start instant for a one-off schedule (else None).
    def one_off_start(self) -> Optional[DateTime]:
        return self._one_off_start_utc()

    # Absolute UTC start instant for a one-off schedule, used wherever the
    # window must be resolved (trigger scheduling and the "is it due now?"
    # checks). Returns None for recurring schedules.
    def _one_off_start_utc(self) -> Optional[DateTime]:
        if not (self.one_off and self.start_date):
            return None
        y, mo, d = (int(p) for p in self.start_date.split("-"))
        if self.start_time_local is not None:
            # `start_date` is a local calendar date and `start_time_local` the
            # local wall-clock time. Build the instant in the local zone and
            # convert to UTC, so a start before the local midnight boundary
            # (e.g. 01:30 Berlin = 23:30 UTC the previous day) lands on the
            # correct UTC day.
            start = pendulum.datetime(
                y,
                mo,
                d,
                self.start_time_local.hour,
                self.start_time_local.minute,
                self.start_time_local.second,
                tz=settings.TIME_ZONE,
            )
            return start.astimezone(pendulum.timezone("UTC"))
        # Legacy schedules (built before start_time_local existed): the stored
        # start_timeofday is already a UTC wall-clock time; combine it with the
        # local date as if it were a UTC date.
        return pendulum.datetime(
            y, mo, d,
            self.start_timeofday.hour,
            self.start_timeofday.minute,
            self.start_timeofday.second,
            tz="UTC",
        )

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
        one_off_start = self._one_off_start_utc()
        if one_off_start is not None:
            # Single occurrence: a single fixed window in UTC, mirroring how
            # the one-off DateTrigger fires at one_off_start().
            return TimePeriod(
                start=one_off_start,
                end=one_off_start + self.duration.as_timedelta(),
            )

        # Find the recording window (start, start+duration) that contains the
        # given time, resolving the cron in the same zone the trigger uses
        # (local wall-clock time when available, UTC otherwise) so that DST
        # shifts and the UTC/local midnight boundary are handled correctly.
        # croniter's get_prev/get_next are exclusive of an exact match, so at
        # the precise fire time (second 0, as produced by the cron trigger)
        # they would both skip the current day and return yesterday and
        # tomorrow. Detect an exact match first, otherwise a recording that
        # starts right at its scheduled time would resolve to the next day and
        # wait ~24h before actually recording.
        start_local_time, tz_name = self.cron_start()
        tz = pendulum.timezone(tz_name)
        expr = self.cron_expression
        duration = self.duration.as_timedelta()

        now_local = recording_start_time.in_tz(tz)
        fire_time_local = now_local.replace(second=0, microsecond=0)
        # croniter.match is a classmethod here; it honours the day-of-week too.
        if croniter.match(expr, fire_time_local):
            start_utc = fire_time_local.in_tz("UTC")
        else:
            # croniter returns naive datetimes even for aware inputs; they
            # already hold the local wall-clock time, so re-attach the zone.
            cron = croniter(expr, start_time=now_local)
            prev_start_local = pendulum.instance(cron.get_prev(datetime), tz=tz)

            # Check if we are still within the prev recording period
            if prev_start_local <= now_local < prev_start_local + duration:
                # Recording has been started during the previous period (e.g.
                # the app was restarted while a recording was active).
                logger.debug(
                    f"Schedule '{self.title}': Recording has been started during previous recording period"
                )
                start_utc = prev_start_local.in_tz("UTC")
            else:
                start_utc = pendulum.instance(
                    cron.get_next(datetime), tz=tz
                ).in_tz("UTC")

        return TimePeriod(start=start_utc, end=start_utc + duration)
