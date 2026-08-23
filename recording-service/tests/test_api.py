import urllib.parse
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from pendulum import Duration

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

