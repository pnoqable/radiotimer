import urllib.parse
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src import settings


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

        # path traversal must be rejected
        evil = "/api/recordings?path=" + urllib.parse.quote("../../etc/passwd")
        res2 = client.delete(evil)
        assert res2.status_code == 400

        # deleting a non-existent file returns 404
        missing = "/api/recordings?path=" + urllib.parse.quote("nope.mp3")
        assert client.delete(missing).status_code == 404
