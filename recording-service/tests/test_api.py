import urllib.parse
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient
import pendulum
from pendulum import DateTime, Duration

from src import db, settings, utils
from src.schedule_builder import build_schedule


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
            assert job_status["live_url"].startswith("/api/recordings/live?path=")
            assert "BR/S/live.mp3" in job_status["live_url"]
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
        res = client.get("/api/recordings/live?path=" + urllib.parse.quote(rel))
        assert res.status_code == 200
        assert res.content == b"hello"


def test_recordings_live_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
    import main as m

    with TestClient(m.app) as client:
        res = client.get(
            "/api/recordings/live?path=" + urllib.parse.quote("../../etc/passwd")
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
        assert "http://example.com/api/recordings/live?path=" in body
        assert urllib.parse.quote(rel) in body
        assert "2026-08-10 23-05" in body


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

