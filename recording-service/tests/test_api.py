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


def test_start_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
    import main as m

    db.init_db()
    station = db.create_station({"name": "BR", "url": "http://x/y.m3u"})
    now = utils.TimeProvider().get_current_time()
    # build_schedule interprets time strings in the system's local timezone and
    # converts to UTC, so derive that offset (in hours) from a probe schedule.
    from src.schedule_builder import build_schedule

    probe = db.create_schedule(
        {
            "title": "probe",
            "station_id": station["id"],
            "start_time": "12:00",
            "end_time": "12:01",
            "frequency": "*",
        }
    )
    probe_sch = build_schedule(db.get_schedule(probe["id"]))
    offset = 12 - probe_sch.start_timeofday.hour
    local_now = now + Duration(hours=offset)
    due = db.create_schedule(
        {
            "title": "Laeuft",
            "station_id": station["id"],
            "start_time": (local_now - Duration(minutes=5)).format("HH:mm"),
            "end_time": (local_now + Duration(minutes=5)).format("HH:mm"),
            "frequency": "*",
        }
    )
    later = db.create_schedule(
        {
            "title": "Spaeter",
            "station_id": station["id"],
            "start_time": (local_now + Duration(hours=12)).format("HH:mm"),
            "end_time": (local_now + Duration(hours=12) + Duration(minutes=1)).format("HH:mm"),
            "frequency": "*",
        }
    )

    # Avoid actually starting ffmpeg: replace the scheduler with a mock.
    sched_mock = mock.MagicMock()
    job = mock.MagicMock()
    job.id = due["id"]
    sched_mock.get_jobs.return_value = [job]
    monkeypatch.setattr(m.scheduler_service, "scheduler", sched_mock)

    with TestClient(m.app) as client:
        # status reports "due" for the in-window schedule
        status = client.get("/api/status").json()
        assert any(j["id"] == due["id"] and j["due"] for j in status["jobs"])

        # starting an in-window schedule succeeds
        res = client.post(f"/api/recordings/{due['id']}/start")
        assert res.status_code == 200
        assert res.json() == {"started": True}
        sched_mock.modify_job.assert_called_once()

        # starting a schedule outside its window is rejected
        res2 = client.post(f"/api/recordings/{later['id']}/start")
        assert res2.status_code == 400

        # when already running, the endpoint reports that instead
        monkeypatch.setattr(m, "is_active", lambda _id: True)
        res3 = client.post(f"/api/recordings/{due['id']}/start")
        assert res3.status_code == 200
        assert res3.json() == {"started": False, "reason": "already running"}

