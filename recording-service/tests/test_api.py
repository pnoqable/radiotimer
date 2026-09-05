import time
import urllib.parse
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient
import pendulum
from pendulum import DateTime, Duration

from src import db, settings, signals, utils
from src.schedule_builder import build_schedule


def test_signals_version_bump():
    before = signals.version()
    signals.bump()
    assert signals.version() == before + 1


def test_signals_subscribe_unsubscribe():
    ev = signals.subscribe()
    assert ev in signals._subscribers
    signals.unsubscribe(ev)
    assert ev not in signals._subscribers


def test_bump_reaches_subscribers_outside_event_loop():
    # Regression: synchronous endpoints (delete recording/station/schedule) run
    # in a threadpool without a running loop, so _broadcast must use the loop
    # captured at startup via set_loop(). Otherwise every push was silently
    # dropped and only the acting page (which reloads itself) updated.
    import main as m

    with TestClient(m.app) as client:
        ev = signals.subscribe()
        try:
            signals.bump()
            # The loop captured at startup processes the scheduled
            # put_nowait; give its thread a moment, then read non-blocking.
            time.sleep(0.3)
            assert ev.get_nowait() == ("state",)
        finally:
            signals.unsubscribe(ev)


def test_station_and_schedule_deletes_broadcast():
    # Every state-changing endpoint must bump(), so all SSE clients (including
    # the acting page) learn about the change via push instead of a local reload.
    import main as m
    from src import db as dbmod

    with TestClient(m.app) as client:
        station = dbmod.create_station({"name": "T", "url": "http://x/y.m3u"})
        sched = dbmod.create_schedule(
            {
                "title": "S",
                "station_id": station["id"],
                "start_time": "18:00",
                "end_time": "19:00",
            }
        )
        ev = signals.subscribe()
        try:
            time.sleep(0.3)  # flush any events queued before subscribing
            r = client.delete(f"/api/schedules/{sched['id']}")
            assert r.status_code == 200
            time.sleep(0.3)
            assert ev.get_nowait() == ("state",)

            r = client.delete(f"/api/stations/{station['id']}")
            assert r.status_code == 200
            time.sleep(0.3)
            assert ev.get_nowait() == ("state",)
        finally:
            signals.unsubscribe(ev)


def test_resolve_recording_period_exact_boundary_is_today():
    # At the precise scheduled time (second 0, as the cron trigger fires it)
    # the window must resolve to *today*, not tomorrow. Otherwise the
    # recording waits ~24h and never starts on time.
    from datetime import datetime as _dt

    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "title": "T",
        "station_name": "S",
        "station_url": "http://x",
        "start_time": "14:00",
        "end_time": "15:00",
        "frequency": "*",
    }
    s = build_schedule(row)
    fire = pendulum.datetime(2026, 8, 24, 12, 0, 0, tz="UTC")
    period = s.resolve_recording_period(fire)
    assert period.start == _dt(2026, 8, 24, 12, 0, 0, tzinfo=pendulum.tz.UTC)
    # 14:30 (inside the window) and 15:30 (after it) must stay consistent too.
    assert s.resolve_recording_period(
        pendulum.datetime(2026, 8, 24, 12, 30, 0, tz="UTC")
    ).start.day == 24
    assert s.resolve_recording_period(
        pendulum.datetime(2026, 8, 24, 13, 30, 0, tz="UTC")
    ).start.day == 25


def test_delete_recording(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")

    import main as m

    with TestClient(m.app) as client:
        rec = tmp_path / "BR Klassik" / "Jazz" / "2026-08-10 23-05.mp3"
        rec.parent.mkdir(parents=True)
        rec.write_text("data")
        assert rec.exists()

        # delete the file (path with spaces/slashes, as the UI sends it)
        url = "/api/recordings?path=" + urllib.parse.quote(str(Path("BR Klassik") / "Jazz" / "2026-08-10 23-05.mp3"))
        res = client.delete(url)
        assert res.status_code == 200
        assert not rec.exists()
        # empty parent folders are pruned automatically
        assert not (tmp_path / "BR Klassik" / "Jazz").exists()
        assert not (tmp_path / "BR Klassik").exists()

        # path traversal must be rejected
        evil = "/api/recordings?path=" + urllib.parse.quote("../../etc/passwd")
        res2 = client.delete(evil)
        assert res2.status_code == 400

        # deleting a non-existent file returns 404
        missing = "/api/recordings?path=" + urllib.parse.quote("nope.mp3")
        assert client.delete(missing).status_code == 404


def test_disable_schedule_stops_recording(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
    import main as m

    db.init_db()
    station = db.create_station({"name": "BR", "url": "http://x/y.m3u"})
    sched = db.create_schedule(
        {
            "title": "S",
            "station_id": station["id"],
            "start_time": "12:00",
            "end_time": "12:01",
            "frequency": "*",
        }
    )

    sched_mock = mock.MagicMock()
    monkeypatch.setattr(m.scheduler_service, "scheduler", sched_mock)
    stop_mock = mock.MagicMock(return_value=True)
    monkeypatch.setattr(m, "stop", stop_mock)

    with TestClient(m.app) as client:
        payload = db.get_schedule(sched["id"])
        payload["enabled"] = False
        res = client.put(f"/api/schedules/{sched['id']}", json=payload)
        assert res.status_code == 200
        assert res.json()["enabled"] is False
        # Disabling a schedule must stop any in-progress recording for it.
        stop_mock.assert_called_once_with(sched["id"])


def test_toggle_schedule_disables_and_enables(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
    import main as m

    db.init_db()
    station = db.create_station({"name": "BR", "url": "http://x/y.m3u"})
    sched = db.create_schedule(
        {
            "title": "S",
            "station_id": station["id"],
            "start_time": "12:00",
            "end_time": "12:01",
            "frequency": "*",
        }
    )

    sched_mock = mock.MagicMock()
    monkeypatch.setattr(m.scheduler_service, "scheduler", sched_mock)

    with TestClient(m.app) as client:
        # startup loaded the enabled schedule into the scheduler
        assert sched_mock.add_job.call_count >= 1

        # disable it -> the job must be removed and not re-added
        payload = db.get_schedule(sched["id"])
        payload["enabled"] = False
        res = client.put(f"/api/schedules/{sched['id']}", json=payload)
        assert res.status_code == 200
        assert res.json()["enabled"] is False
        sched_mock.remove_job.assert_called_with(sched["id"])
        add_after_disable = sched_mock.add_job.call_count

        # re-enable it -> the job is added back
        payload["enabled"] = True
        res2 = client.put(f"/api/schedules/{sched['id']}", json=payload)
        assert res2.status_code == 200
        assert res2.json()["enabled"] is True
        assert sched_mock.add_job.call_count == add_after_disable + 1


def test_delete_schedule_stops_recording(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
    import main as m

    db.init_db()
    station = db.create_station({"name": "BR", "url": "http://x/y.m3u"})
    sched = db.create_schedule(
        {
            "title": "S",
            "station_id": station["id"],
            "start_time": "12:00",
            "end_time": "12:01",
            "frequency": "*",
        }
    )

    stop_spy = mock.MagicMock()
    monkeypatch.setattr(m, "stop", stop_spy)

    with TestClient(m.app) as client:
        res = client.delete(f"/api/schedules/{sched['id']}")
        assert res.status_code == 200
        stop_spy.assert_called_once_with(sched["id"])

        # schedule is gone afterwards
        assert client.get(f"/api/schedules/{sched['id']}").status_code == 404


def test_reload_job_stops_recording_when_window_moved_out(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(settings, "TIME_ZONE", "UTC")
    import main as m
    from src import ffmpeg_recorder as fr

    db.init_db()
    station = db.create_station({"name": "BR", "url": "http://x/y.m3u"})
    sched = db.create_schedule(
        {
            "title": "S",
            "station_id": station["id"],
            "start_time": "12:00",
            "end_time": "12:01",
            "frequency": "*",
        }
    )

    # Fix "now" outside the edited window.
    monkeypatch.setattr(m.utils, "get_utc_now", lambda: pendulum.parse("2020-01-01T12:00:00+00:00"))

    # Pretend the recording is currently running.
    fr._active[sched["id"]] = mock.MagicMock(returncode=None)
    stop_spy = mock.MagicMock()
    monkeypatch.setattr(m, "stop", stop_spy)
    monkeypatch.setattr(m.scheduler_service, "scheduler", mock.MagicMock())

    # Edit the window so it no longer covers "now" (03:00-03:01).
    db.update_schedule(sched["id"], {**sched, "start_time": "03:00", "end_time": "03:01"})
    m.reload_job(sched["id"])

    stop_spy.assert_called_once_with(sched["id"])
    fr._active.pop(sched["id"], None)


def test_reload_job_keeps_recording_when_still_in_window(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(settings, "TIME_ZONE", "UTC")
    import main as m
    from src import ffmpeg_recorder as fr

    db.init_db()
    station = db.create_station({"name": "BR", "url": "http://x/y.m3u"})
    sched = db.create_schedule(
        {
            "title": "S",
            "station_id": station["id"],
            "start_time": "12:00",
            "end_time": "12:01",
            "frequency": "*",
        }
    )

    # Fix "now" inside the (unchanged) window.
    monkeypatch.setattr(m.utils, "get_utc_now", lambda: pendulum.parse("2020-01-01T12:00:30+00:00"))

    fr._active[sched["id"]] = mock.MagicMock(returncode=None)
    stop_spy = mock.MagicMock()
    monkeypatch.setattr(m, "stop", stop_spy)
    monkeypatch.setattr(m.scheduler_service, "scheduler", mock.MagicMock())

    db.update_schedule(sched["id"], {**sched, "title": "S2"})
    m.reload_job(sched["id"])

    stop_spy.assert_not_called()
    fr._active.pop(sched["id"], None)


def test_recordings_tree_marks_live_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
    import main as m
    from src import ffmpeg_recorder as fr

    live_file = tmp_path / "BR" / "live.mp3"
    live_file.parent.mkdir(parents=True)
    live_file.write_bytes(b"x")
    finished = tmp_path / "BR" / "old.mp3"
    finished.write_bytes(b"x")

    fr._paths["fake"] = live_file
    try:
        with TestClient(m.app) as client:
            tree = client.get("/api/recordings").json()["tree"]
            files = {f["name"]: f for f in tree["children"][0]["children"]}
            assert files["live.mp3"]["live"] is True
            assert files["old.mp3"]["live"] is False
    finally:
        fr._paths.pop("fake", None)


def test_open_station_redirects_to_stream(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
    import main as m

    # Avoid real network access: stub the resolver.
    async def fake_resolve(url):
        return "http://stream/resolved.mp3"

    monkeypatch.setattr(m, "resolve_stream_url", fake_resolve)
    db.init_db()
    station = db.create_station({"name": "BR", "url": "http://x/y.m3u"})

    with TestClient(m.app) as client:
        res = client.get(f"/api/stations/{station['id']}/open", follow_redirects=False)
        assert res.status_code in (302, 307)
        assert res.headers["location"] == "http://stream/resolved.mp3"

        # unknown station -> 404
        assert client.get("/api/stations/nope/open", follow_redirects=False).status_code == 404


def test_status_includes_live_url_for_running(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
    import main as m
    from src import ffmpeg_recorder as fr

    db.init_db()
    station = db.create_station({"name": "BR", "url": "http://x/y.m3u"})
    sched = db.create_schedule(
        {
            "title": "S",
            "station_id": station["id"],
            "start_time": "12:00",
            "end_time": "12:01",
            "frequency": "*",
        }
    )

    job = mock.MagicMock()
    job.id = sched["id"]
    job.next_run_time = None
    sched_mock = mock.MagicMock()
    sched_mock.get_jobs.return_value = [job]
    monkeypatch.setattr(m.scheduler_service, "scheduler", sched_mock)
    monkeypatch.setattr(m, "is_active", lambda _id: True)

    # Pretend this schedule is currently being recorded into this file.
    live_file = tmp_path / "BR" / "S" / "live.mp3"
    fr._paths[sched["id"]] = live_file

    try:
        with TestClient(m.app) as client:
            status = client.get("/api/status").json()
            job_status = next(j for j in status["jobs"] if j["id"] == sched["id"])
            assert job_status["running"] is True
            assert job_status["live_url"] is not None
            assert job_status["live_url"].startswith("/api/recordings/play?path=")
            assert "BR/S/live.mp3" in job_status["live_url"]
    finally:
        fr._paths.pop(sched["id"], None)


def test_active_one_off_shows_running_without_scheduler_job(tmp_path, monkeypatch):
    # A one-off DateTrigger job is removed by APScheduler once it fires, so it
    # is no longer in get_jobs() while the capture is still in progress. It must
    # still be reported as running (with a live URL) in the status endpoint.
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
    import main as m

    from src import ffmpeg_recorder as fr

    db.init_db()
    station = db.create_station({"name": "BR", "url": "http://x/y.m3u"})
    sched = db.create_schedule(
        {
            "title": "Sonderfolge",
            "station_id": station["id"],
            "start_time": "20:00",
            "end_time": "21:00",
            "frequency": "",
            "one_off": True,
            "start_date": "2030-12-24",
        }
    )

    sched_mock = mock.MagicMock()
    sched_mock.get_jobs.return_value = []
    monkeypatch.setattr(m.scheduler_service, "scheduler", sched_mock)
    monkeypatch.setattr(m, "is_active", lambda _id: True)

    live_file = tmp_path / "BR" / "Sonderfolge" / "live.mp3"
    fr._paths[sched["id"]] = live_file

    try:
        with TestClient(m.app) as client:
            status = client.get("/api/status").json()
            job_status = next(j for j in status["jobs"] if j["id"] == sched["id"])
            assert job_status["running"] is True
            assert job_status["live_url"] is not None
            assert "BR/Sonderfolge/live.mp3" in job_status["live_url"]
    finally:
        fr._paths.pop(sched["id"], None)


def test_live_file_follows_growth(tmp_path, monkeypatch):
    import asyncio

    from src import ffmpeg_recorder as fr

    path = tmp_path / "live.mp3"
    path.write_bytes(b"AAAA")
    # Mark the file as currently being recorded.
    fr._paths["fake"] = path
    try:

        async def collect():
            got = b""
            async for chunk in fr.iter_live_file(path):
                got += chunk
                if got == b"AAAA":
                    # Recording just finished: stop being "live" and append more.
                    fr._paths.pop("fake", None)
                    with open(path, "ab") as f:
                        f.write(b"BBBB")
            return got

        result = asyncio.run(collect())
        # The stream must follow the growing file and then stop once the
        # recording has ended.
        assert result == b"AAAABBBB"
    finally:
        fr._paths.pop("fake", None)


def test_recordings_live_serves_static_when_not_recording(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
    import main as m

    rec = tmp_path / "S" / "show.mp3"
    rec.parent.mkdir(parents=True)
    rec.write_bytes(b"hello")
    rel = rec.relative_to(tmp_path).as_posix()

    with TestClient(m.app) as client:
        res = client.get("/api/recordings/play?path=" + urllib.parse.quote(rel))
        assert res.status_code == 200
        assert res.content == b"hello"


def test_recordings_play_suggests_download_name(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
    import main as m

    rec = tmp_path / "BR Klassik" / "Classic Sounds in Jazz" / "2026-08-26 19-05.mp3"
    rec.parent.mkdir(parents=True)
    rec.write_bytes(b"data")
    rel = rec.relative_to(tmp_path).as_posix()

    with TestClient(m.app) as client:
        res = client.get("/api/recordings/play?path=" + urllib.parse.quote(rel))
        assert res.status_code == 200
        cd = res.headers["content-disposition"]
        # The name mirrors the podcast title: file stem + folder parts reversed
        # (RFC 5987-encoded spaces/commas in the filename*= form).
        assert cd.startswith("inline")
        assert "2026-08-26%2019-05%20Classic%20Sounds%20in%20Jazz%2C%20BR%20Klassik.mp3" in cd


def test_recordings_live_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
    import main as m

    with TestClient(m.app) as client:
        res = client.get(
            "/api/recordings/play?path=" + urllib.parse.quote("../../etc/passwd")
        )
        assert res.status_code == 400


def test_podcast_feed_lists_recordings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(settings, "PUBLIC_URL", "http://example.com")
    import main as m

    rec = tmp_path / "BR" / "Jazz" / "2026-08-10 23-05.mp3"
    rec.parent.mkdir(parents=True)
    rec.write_bytes(b"data")
    rel = rec.relative_to(tmp_path).as_posix()

    with TestClient(m.app) as client:
        res = client.get(
            "/api/podcast?folder=" + urllib.parse.quote("BR/Jazz"),
        )
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("application/rss+xml")
        body = res.text
        assert "<rss" in body and "<channel>" in body
        # Enclosure points at the static recording endpoint with an absolute URL.
        assert "http://example.com/api/recordings/play?path=" in body
        assert urllib.parse.quote(rel) in body
        # Episode title joins the file name with the folder parts in reverse.
        assert "2026-08-10 23-05 Jazz, BR" in body


def test_podcast_feed_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
    import main as m

    with TestClient(m.app) as client:
        res = client.get(
            "/api/podcast?folder=" + urllib.parse.quote("../../etc")
        )
        assert res.status_code == 400


def test_podcast_feed_404_for_missing_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
    import main as m

    with TestClient(m.app) as client:
        res = client.get("/api/podcast?folder=" + urllib.parse.quote("nope"))
        assert res.status_code == 404


def test_update_without_enabled_keeps_state(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
    import main as m

    db.init_db()
    station = db.create_station({"name": "BR", "url": "http://x/y.m3u"})
    sched = db.create_schedule(
        {
            "title": "S",
            "station_id": station["id"],
            "start_time": "12:00",
            "end_time": "12:01",
            "frequency": "*",
        }
    )

    sched_mock = mock.MagicMock()
    monkeypatch.setattr(m.scheduler_service, "scheduler", sched_mock)

    with TestClient(m.app) as client:
        # Pause it via the toggle (full payload includes enabled).
        cur = db.get_schedule(sched["id"])
        cur["enabled"] = False
        res_pause = client.put(f"/api/schedules/{sched['id']}", json=cur)
        assert res_pause.status_code == 200
        assert res_pause.json()["enabled"] is False

        # Edit the title only (no enabled field) -> must stay paused.
        res = client.put(
            f"/api/schedules/{sched['id']}",
            json={
                "title": "S2",
                "station_id": station["id"],
                "start_time": "12:00",
                "end_time": "12:01",
            },
        )
        assert res.status_code == 200
        assert res.json()["enabled"] is False
        assert res.json()["title"] == "S2"


def test_api_disk_reports_usage():
    import main as m

    with TestClient(m.app) as client:
        res = client.get("/api/disk")
        assert res.status_code == 200
        data = res.json()
        assert {"path", "total", "used", "free"} <= data.keys()
        assert data["total"] >= data["used"]
        assert data["free"] == data["total"] - data["used"]
        assert data["path"] == str(settings.OUTPUT_DIR.resolve())


def _make_one_off_payload(station_id, start_date):
    return {
        "title": "Sonderfolge",
        "station_id": station_id,
        "start_time": "20:00",
        "end_time": "21:00",
        "frequency": "",
        "one_off": True,
        "start_date": start_date,
    }


def test_create_one_off_schedule(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
    import main as m

    db.init_db()
    station = db.create_station({"name": "BR", "url": "http://x/y.m3u"})

    with TestClient(m.app) as client:
        res = client.post(
            "/api/schedules", json=_make_one_off_payload(station["id"], "2030-12-24")
        )
        assert res.status_code == 200
        d = res.json()
        assert d["one_off"] is True
        assert d["start_date"] == "2030-12-24"

