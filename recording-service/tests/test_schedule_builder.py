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


def test_recording_task_path_uses_pattern(monkeypatch, tmp_path):
    # Adopted from the old "VLC Timer": <station>/<title>/<date> <HH-MM>.mp3
    monkeypatch.setattr(settings, "PATTERN", "{station}/{title}/{date} {start_hm}.{ext}")

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

    assert task.file_path == tmp_path / "br-klassik" / "testsendung" / "2026-01-02 18-00.mp3"


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
