import asyncio
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


def test_not_due_schedule_job_gets_a_next_run_time(tmp_path, monkeypatch):
    # Regression test for d684771: a future (not-due) schedule must actually be
    # scheduled by APScheduler. Passing next_run_time=None explicitly left the
    # job's next_run_time unset, so it never fired.
    fixed = pendulum.datetime(2026, 1, 2, 22, 0, 0, tz="UTC")
    svc = _svc()
    svc._time_provider.get_current_time = lambda: fixed

    sched = _make_schedule(tmp_path)
    svc.add_recording_schedule(sched)

    async def check():
        svc.scheduler.start()
        try:
            job = svc.scheduler.get_jobs()[0]
            assert job.next_run_time is not None
            assert job.next_run_time > fixed
        finally:
            svc.scheduler.shutdown(wait=False)

    asyncio.run(check())


def test_one_off_uses_date_trigger(tmp_path):
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.date import DateTrigger

    svc = _svc()

    recurring = _make_schedule(tmp_path)
    assert isinstance(svc._get_trigger(recurring), CronTrigger)

    once = RecordingSchedule(
        title="T",
        station_name="S",
        station_url="http://example.com/stream.m3u",
        start_timeofday=pendulum.time(20, 0),
        duration=pendulum.duration(hours=1),
        audio_format="mp3",
        output_dir=tmp_path,
        one_off=True,
        start_date="2026-12-24",
    )
    assert isinstance(svc._get_trigger(once), DateTrigger)


def test_recurring_trigger_uses_local_timezone(tmp_path):
    from apscheduler.triggers.cron import CronTrigger

    svc = _svc()

    monday_night = RecordingSchedule(
        title="T",
        station_name="S",
        station_url="http://example.com/stream.m3u",
        start_timeofday=pendulum.time(23, 30),  # UTC-shifted time (old behaviour)
        duration=pendulum.duration(hours=1),
        audio_format="mp3",
        output_dir=tmp_path,
        frequency="mon",
        start_time_local=pendulum.time(1, 30),  # local wall-clock time wins
    )
    tr = svc._get_trigger(monday_night)
    assert isinstance(tr, CronTrigger)
    # The cron runs in the local zone at 01:30, so Monday 01:30 Berlin fires
    # at Sunday 23:30 UTC during summer (CEST) instead of a UTC-shifted time.
    assert str(tr.timezone) == "Europe/Berlin"
    ur = tr.get_next_fire_time(None, pendulum.datetime(2026, 7, 1, tz="UTC"))
    assert ur == pendulum.datetime(2026, 7, 5, 23, 30, 0, tz="UTC")


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
