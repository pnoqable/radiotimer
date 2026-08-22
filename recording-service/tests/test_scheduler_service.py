import pendulum

from src import utils
from src.audio_storage import AudioStorageAdapter
from src.models import RecordingSchedule
from src.recording_service import RecordAudioService
from src.scheduler_service import RecordingSchedulerService


def _make_schedule(tmp_path):
    return RecordingSchedule(
        title="T",
        station_name="S",
        station_url="http://example.com/stream.m3u",
        start_timeofday=pendulum.time(20, 0),
        duration=pendulum.duration(hours=1),
        audio_format="mp3",
        output_dir=tmp_path,
    )


def _svc():
    return RecordingSchedulerService(
        RecordAudioService(AudioStorageAdapter(), utils.TimeProvider()),
        utils.TimeProvider(),
        "mp3",
    )


def test_due_schedule_starts_immediately(tmp_path, monkeypatch):
    fixed = pendulum.datetime(2026, 1, 2, 20, 30, 0, tz="UTC")
    monkeypatch.setattr(utils, "get_utc_now", lambda: fixed)

    svc = _svc()
    calls = []
    svc._add_job = lambda s, n=None: calls.append(n)

    sched = _make_schedule(tmp_path)
    svc.add_recording_schedule(sched)

    assert len(calls) == 1
    nrt = calls[0]
    assert nrt is not None
    delta = nrt.timestamp() - fixed.timestamp()
    assert 0 <= delta <= 10


def test_not_due_schedule_runs_next_occurrence(tmp_path, monkeypatch):
    fixed = pendulum.datetime(2026, 1, 2, 22, 0, 0, tz="UTC")
    monkeypatch.setattr(utils, "get_utc_now", lambda: fixed)

    svc = _svc()
    calls = []
    svc._add_job = lambda s, n=None: calls.append(n)

    sched = _make_schedule(tmp_path)
    svc.add_recording_schedule(sched)

    assert calls == [None]


def test_active_schedule_is_not_started_again(tmp_path, monkeypatch):
    fixed = pendulum.datetime(2026, 1, 2, 20, 30, 0, tz="UTC")
    monkeypatch.setattr(utils, "get_utc_now", lambda: fixed)

    svc = _svc()
    calls = []
    svc._add_job = lambda s, n=None: calls.append(n)

    sched = _make_schedule(tmp_path)
    # First add: not yet active -> should schedule immediate start.
    svc.add_recording_schedule(sched)
    assert calls[-1] is not None

    # Pretend the schedule is already recording.
    from src import ffmpeg_recorder

    class _FakeProc:
        returncode = None

    ffmpeg_recorder._active[str(sched.id)] = _FakeProc()

    # Re-adding (e.g. on edit) must NOT schedule a second immediate run.
    svc.add_recording_schedule(sched)
    assert calls[-1] is None

    del ffmpeg_recorder._active[str(sched.id)]
