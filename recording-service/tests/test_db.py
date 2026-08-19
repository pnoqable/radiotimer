import uuid

from src import db, settings


def test_crud_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
    db.init_db()

    created = db.create_schedule(
        {
            "title": "Testsendung",
            "stream_url": "http://example.com/stream.mp3",
            "start_time": "20:00",
            "end_time": "21:00",
            "subdir": "test",
            "frequency": "mon-fri",
        }
    )
    assert created["id"]
    assert created["enabled"] is True

    # list + get
    assert len(db.list_schedules()) == 1
    fetched = db.get_schedule(created["id"])
    assert fetched is not None
    assert fetched["title"] == "Testsendung"

    # update
    updated = db.update_schedule(
        created["id"], {**created, "title": "Testsendung 2", "enabled": False}
    )
    assert updated["title"] == "Testsendung 2"
    assert updated["enabled"] is False

    # delete
    assert db.delete_schedule(created["id"]) is True
    assert db.get_schedule(created["id"]) is None
    assert db.list_schedules() == []
