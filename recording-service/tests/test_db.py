import uuid

from src import db, settings


def test_station_crud(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
    db.init_db()

    created = db.create_station({"name": "BR", "url": "http://x/y.m3u"})
    assert created["id"]
    assert created["enabled"] is True
    assert len(db.list_stations()) == 1

    updated = db.update_station(
        created["id"], {"name": "BR2", "url": "http://z/w.m3u", "enabled": False}
    )
    assert updated["name"] == "BR2"
    assert updated["enabled"] is False

    assert db.delete_station(created["id"]) is True
    assert db.get_station(created["id"]) is None


def test_schedule_references_station(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
    db.init_db()

    station = db.create_station({"name": "BR", "url": "http://x/y.m3u"})
    created = db.create_schedule(
        {
            "title": "Testsendung",
            "station_id": station["id"],
            "start_time": "20:00",
            "end_time": "21:00",
            "frequency": "mon-fri",
        }
    )
    assert created["id"]
    assert created["station_name"] == "BR"
    assert created["station_url"] == "http://x/y.m3u"
    assert created["enabled"] is True

    # list + get
    assert len(db.list_schedules()) == 1
    fetched = db.get_schedule(created["id"])
    assert fetched is not None
    assert fetched["title"] == "Testsendung"

    # update
    db.update_schedule(created["id"], {**created, "title": "Testsendung 2", "enabled": False})
    assert db.get_schedule(created["id"])["title"] == "Testsendung 2"
    assert db.get_schedule(created["id"])["enabled"] is False

    # delete blocked while a schedule references the station
    try:
        db.delete_station(station["id"])
        assert False, "delete_station should have raised"
    except ValueError:
        pass

    # delete schedule, then station is deletable
    assert db.delete_schedule(created["id"]) is True
    assert db.delete_station(station["id"]) is True

    # unknown station_id is rejected on create
    try:
        db.create_schedule(
            {
                "title": "X",
                "station_id": "does-not-exist",
                "start_time": "20:00",
                "end_time": "21:00",
            }
        )
        assert False, "create_schedule should have raised"
    except ValueError:
        pass
