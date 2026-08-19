import uuid

from src import settings
from src import schedule_builder


def test_build_schedule_converts_local_time_to_utc(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "TIME_ZONE", "Europe/Berlin")
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)

    schedule_id = str(uuid.uuid4())
    row = {
        "id": schedule_id,
        "title": "Abendshow",
        "stream_url": "http://example.com/stream.mp3",
        "start_time": "20:00",
        "end_time": "21:00",
        "frequency": "mon-fri",
        "audio_format": "mp3",
        "description": None,
    }

    schedule = schedule_builder.build_schedule(row)

    # 20:00 Europe/Berlin == 18:00 UTC
    assert schedule.start_timeofday.hour == 18
    assert schedule.start_timeofday.minute == 0
    # One hour duration
    assert schedule.duration.in_seconds() == 3600
    # Per-show stream URL is carried over
    assert str(schedule.stream_url) == "http://example.com/stream.mp3"
    # Output dir uses the slugified title
    assert schedule.output_dir == tmp_path / "abendshow"
    # Schedule id aligned with DB id so the scheduler job can be matched
    assert str(schedule.id) == schedule_id


def test_build_schedule_midnight_wrap(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "TIME_ZONE", "Europe/Berlin")
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)

    row = {
        "id": str(uuid.uuid4()),
        "title": "Nacht",
        "stream_url": "http://example.com/stream.mp3",
        "start_time": "23:00",
        "end_time": "01:00",
        "frequency": "*",
        "audio_format": "mp3",
        "description": None,
    }
    schedule = schedule_builder.build_schedule(row)
    # 23:00-01:00 local is a 2 hour duration across midnight
    assert schedule.duration.in_seconds() == 7200
