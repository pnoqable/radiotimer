import uuid

import pendulum
import pytest

from src import settings
from src import schedule_builder
from src.models import RecordingTask, ValidUrl
from src import utils


def test_build_schedule_converts_local_time_to_utc(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "TIME_ZONE", "Europe/Berlin")
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)

    schedule_id = str(uuid.uuid4())
    row = {
        "id": schedule_id,
        "title": "Abendshow",
        "station_name": "BR Klassik",
        "station_url": "http://example.com/stream.m3u",
        "start_time": "20:00",
        "end_time": "21:00",
        "frequency": "mon-fri",
        "audio_format": "mp3",
    }

    schedule = schedule_builder.build_schedule(row)

    # 20:00 Europe/Berlin == 18:00 UTC
    assert schedule.start_timeofday.hour == 18
    assert schedule.start_timeofday.minute == 0
    # One hour duration
    assert schedule.duration.in_seconds() == 3600
    # Station info carried over (no direct stream_url on the schedule)
    assert schedule.station_name == "BR Klassik"
    assert schedule.station_url == "http://example.com/stream.m3u"
    # Output dir is the global base dir (pattern decides subfolders)
    assert schedule.output_dir == tmp_path
    # Schedule id aligned with DB id so the scheduler job can be matched
    assert str(schedule.id) == schedule_id


def test_build_schedule_midnight_wrap(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "TIME_ZONE", "Europe/Berlin")
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)

    row = {
        "id": str(uuid.uuid4()),
        "title": "Nacht",
        "station_name": "DLF",
        "station_url": "http://example.com/stream.m3u",
        "start_time": "23:00",
        "end_time": "01:00",
        "frequency": "*",
        "audio_format": "mp3",
    }
    schedule = schedule_builder.build_schedule(row)
    # 23:00-01:00 local is a 2 hour duration across midnight
    assert schedule.duration.in_seconds() == 7200


def test_build_schedule_one_off_window(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "TIME_ZONE", "UTC")
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)

    row = {
        "id": str(uuid.uuid4()),
        "title": "Sonderfolge",
        "station_name": "BR Klassik",
        "station_url": "http://example.com/stream.m3u",
        "start_time": "20:00",
        "end_time": "21:00",
        "frequency": "",
        "audio_format": "mp3",
        "one_off": True,
        "start_date": "2026-12-24",
    }
    schedule = schedule_builder.build_schedule(row)

    assert schedule.one_off is True
    assert schedule.start_date == "2026-12-24"
    # Start fires at the schedule's UTC start time on the given date.
    period = schedule.resolve_recording_period(
        pendulum.datetime(2026, 12, 24, 22, 30, 0, tz="UTC")
    )
    assert period.start == pendulum.datetime(2026, 12, 24, 20, 0, 0, tz="UTC")
    assert period.end == pendulum.datetime(2026, 12, 24, 21, 0, 0, tz="UTC")
    # The absolute UTC start feed to the scheduler matches the window start.
    assert schedule.one_off_start() == pendulum.datetime(
        2026, 12, 24, 20, 0, 0, tz="UTC"
    )


def test_build_schedule_one_off_crossing_utc_midnight_cest(monkeypatch, tmp_path):
    # A one-off between 00:00-02:00 Berlin converts to a UTC time on the
    # PREVIOUS day. It must fire on the correct day (bug: it fired 24h late
    # because the local start date was combined with the shifted UTC time).
    monkeypatch.setattr(settings, "TIME_ZONE", "Europe/Berlin")
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)

    row = {
        "id": str(uuid.uuid4()),
        "title": "Nachtausgabe",
        "station_name": "DLF",
        "station_url": "http://example.com/stream.m3u",
        "start_time": "01:30",
        "end_time": "02:00",
        "frequency": "",
        "audio_format": "mp3",
        "one_off": True,
        "start_date": "2026-09-13",  # CEST (UTC+2)
    }
    schedule = schedule_builder.build_schedule(row)

    # 01:30 Berlin on Sep 13 == 23:30 UTC on Sep 12.
    expected = pendulum.datetime(2026, 9, 12, 23, 30, 0, tz="UTC")
    assert schedule.one_off_start() == expected
    period = schedule.resolve_recording_period(pendulum.now("UTC"))
    assert period.start == expected
    assert period.end == pendulum.datetime(2026, 9, 13, 0, 0, 0, tz="UTC")


def test_build_schedule_one_off_crossing_utc_midnight_cet(monkeypatch, tmp_path):
    # Same, but in winter (CET, UTC+1): 01:30 Berlin == 00:30 UTC the same day.
    monkeypatch.setattr(settings, "TIME_ZONE", "Europe/Berlin")
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)

    row = {
        "id": str(uuid.uuid4()),
        "title": "Nachtausgabe",
        "station_name": "DLF",
        "station_url": "http://example.com/stream.m3u",
        "start_time": "01:30",
        "end_time": "02:00",
        "frequency": "",
        "audio_format": "mp3",
        "one_off": True,
        "start_date": "2026-01-10",  # CET (UTC+1)
    }
    schedule = schedule_builder.build_schedule(row)

    expected = pendulum.datetime(2026, 1, 10, 0, 30, 0, tz="UTC")
    assert schedule.one_off_start() == expected
    assert schedule.resolve_recording_period(pendulum.now("UTC")).start == expected


def test_build_schedule_recurring_cron_uses_local_timezone(monkeypatch, tmp_path):
    # Recurring schedules must fire at the LOCAL wall-clock time. A start that
    # crosses the UTC/local midnight boundary (e.g. Monday 01:30 Berlin) must
    # land on the correct UTC day, and DST is applied per fire date (not at the
    # date the schedule was created).
    monkeypatch.setattr(settings, "TIME_ZONE", "Europe/Berlin")
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)

    row = {
        "id": str(uuid.uuid4()),
        "title": "Nachtausgabe",
        "station_name": "DLF",
        "station_url": "http://example.com/stream.m3u",
        "start_time": "01:30",
        "end_time": "02:00",
        "frequency": "mon",
        "audio_format": "mp3",
    }
    schedule = schedule_builder.build_schedule(row)

    # The cron fires at 01:30 in the local zone, not at the UTC-shifted time.
    assert schedule.cron_start() == (pendulum.time(1, 30), "Europe/Berlin")
    assert schedule.cron_expression == "30 1 * * mon"

    # Monday 01:30 Berlin = Sunday 23:30 UTC in summer (CEST).
    summer = pendulum.datetime(2026, 7, 5, 23, 30, 0, tz="UTC")  # Sun 23:30 UTC
    period = schedule.resolve_recording_period(summer)
    assert period.start == summer
    assert period.end == pendulum.datetime(2026, 7, 6, 0, 0, 0, tz="UTC")

    # A restart mid-window resolves to the window in progress.
    inside = pendulum.datetime(2026, 7, 5, 23, 45, 0, tz="UTC")
    assert schedule.resolve_recording_period(inside).start == summer

    # In winter the same wall clock is CET: Monday 01:30 Berlin = 00:30 UTC.
    winter = pendulum.datetime(2026, 1, 5, 0, 30, 0, tz="UTC")  # Mon 00:30 UTC
    assert schedule.resolve_recording_period(winter).start == winter


def test_recording_task_path_uses_pattern(monkeypatch, tmp_path):
    # Adopted from the old "VLC Timer": <station>/<title>/<date> <HH-MM>.mp3
    monkeypatch.setattr(settings, "PATTERN", "{station}/{title}/{date} {start_hm}.{ext}")
    monkeypatch.setattr(settings, "TIME_ZONE", "UTC")

    start = pendulum.datetime(2026, 1, 2, 18, 0, 0, tz="UTC")
    end = pendulum.datetime(2026, 1, 2, 19, 0, 0, tz="UTC")
    period = utils.TimePeriod(start, end)

    task = RecordingTask(
        title="Testsendung",
        station="BR Klassik",
        recording_period=period,
        base_dir=tmp_path,
        audio_format="mp3",
        stream_url=ValidUrl("http://example.com/stream.mp3"),
    )

    assert task.file_path == tmp_path / "BR Klassik" / "Testsendung" / "2026-01-02 18-00.mp3"


def test_recording_task_does_not_renumber_when_file_exists(tmp_path, monkeypatch):
    # The timestamp reflects the actual recording start, not the schedule's
    # defined start. No sequential counter is appended when the file already
    # exists (ffmpeg's -y overwrites instead).
    monkeypatch.setattr(settings, "PATTERN", "{station}/{title}/{date} {start_hm}.{ext}")
    monkeypatch.setattr(settings, "TIME_ZONE", "UTC")

    start = pendulum.datetime(2026, 1, 2, 18, 0, 0, tz="UTC")
    end = pendulum.datetime(2026, 1, 2, 19, 0, 0, tz="UTC")
    period = utils.TimePeriod(start, end)

    # The original file already exists (e.g. from a prior run / restart).
    first = tmp_path / "BR Klassik" / "Testsendung" / "2026-01-02 18-00.mp3"
    first.parent.mkdir(parents=True)
    first.write_text("x")

    task = RecordingTask(
        title="Testsendung",
        station="BR Klassik",
        recording_period=period,
        base_dir=tmp_path,
        audio_format="mp3",
        stream_url=ValidUrl("http://example.com/stream.mp3"),
    )
    assert task.file_path == (
        tmp_path / "BR Klassik" / "Testsendung" / "2026-01-02 18-00.mp3"
    )


def test_recording_task_path_uses_actual_start(tmp_path, monkeypatch):
    # When an actual start time is supplied (the real recording start), the
    # filename timestamp uses it instead of the schedule's defined start.
    monkeypatch.setattr(settings, "PATTERN", "{station}/{title}/{date} {start_hm}.{ext}")
    monkeypatch.setattr(settings, "TIME_ZONE", "UTC")

    defined = pendulum.datetime(2026, 1, 2, 18, 0, 0, tz="UTC")
    end = pendulum.datetime(2026, 1, 2, 19, 0, 0, tz="UTC")
    period = utils.TimePeriod(defined, end)
    actual = pendulum.datetime(2026, 1, 2, 20, 3, 17, tz="UTC")

    task = RecordingTask(
        title="Testsendung",
        station="BR Klassik",
        recording_period=period,
        base_dir=tmp_path,
        audio_format="mp3",
        stream_url=ValidUrl("http://example.com/stream.mp3"),
        actual_start=actual,
    )
    assert task.file_path == (
        tmp_path / "BR Klassik" / "Testsendung" / "2026-01-02 20-03.mp3"
    )


def test_recording_task_path_uses_local_timezone(tmp_path, monkeypatch):
    # The filename timestamp is rendered in the configured local timezone,
    # not UTC. 20:03 UTC on 2026-01-02 is 15:03 in New York (EST, UTC-5).
    monkeypatch.setattr(settings, "PATTERN", "{station}/{title}/{date} {start_hm}.{ext}")
    monkeypatch.setattr(settings, "TIME_ZONE", "America/New_York")

    defined = pendulum.datetime(2026, 1, 2, 18, 0, 0, tz="UTC")
    end = pendulum.datetime(2026, 1, 2, 19, 0, 0, tz="UTC")
    period = utils.TimePeriod(defined, end)
    actual = pendulum.datetime(2026, 1, 2, 20, 3, 17, tz="UTC")

    task = RecordingTask(
        title="Testsendung",
        station="BR Klassik",
        recording_period=period,
        base_dir=tmp_path,
        audio_format="mp3",
        stream_url=ValidUrl("http://example.com/stream.mp3"),
        actual_start=actual,
    )
    assert task.file_path == (
        tmp_path / "BR Klassik" / "Testsendung" / "2026-01-02 15-03.mp3"
    )


@pytest.mark.asyncio
async def test_schedule_resolves_station_url(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "TIME_ZONE", "Europe/Berlin")
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)

    from src import playlist

    async def fake_resolve(url: str) -> str:
        return "http://example.com/resolved.mp3"

    monkeypatch.setattr(playlist, "resolve_stream_url", fake_resolve)

    row = {
        "id": str(uuid.uuid4()),
        "title": "Abendshow",
        "station_name": "BR Klassik",
        "station_url": "http://example.com/stream.m3u",
        "start_time": "20:00",
        "end_time": "21:00",
        "frequency": "mon-fri",
        "audio_format": "mp3",
    }
    schedule = schedule_builder.build_schedule(row)
    task = await schedule.get_current_or_next_task(utils.get_utc_now())
    assert str(task.stream_url) == "http://example.com/resolved.mp3"
    assert task.station == "BR Klassik"
