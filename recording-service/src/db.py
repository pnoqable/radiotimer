import sqlite3
from pathlib import Path
from typing import Any, Optional

from src import settings


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schedules (
            id           TEXT PRIMARY KEY,
            title        TEXT NOT NULL,
            stream_url   TEXT NOT NULL,
            start_time   TEXT NOT NULL,
            end_time     TEXT NOT NULL,
            frequency    TEXT NOT NULL DEFAULT '*',
            audio_format TEXT NOT NULL DEFAULT 'mp3',
            subdir       TEXT NOT NULL,
            description  TEXT,
            enabled      INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    conn.commit()
    conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "stream_url": row["stream_url"],
        "start_time": row["start_time"],
        "end_time": row["end_time"],
        "frequency": row["frequency"],
        "audio_format": row["audio_format"],
        "subdir": row["subdir"],
        "description": row["description"],
        "enabled": bool(row["enabled"]),
    }


def list_schedules() -> list[dict[str, Any]]:
    conn = _connect()
    rows = conn.execute("SELECT * FROM schedules ORDER BY title").fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_schedule(schedule_id: str) -> Optional[dict[str, Any]]:
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
    ).fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def create_schedule(data: dict[str, Any]) -> dict[str, Any]:
    schedule_id = data.get("id") or str(__import__("uuid").uuid4())
    conn = _connect()
    conn.execute(
        """
        INSERT INTO schedules
            (id, title, stream_url, start_time, end_time, frequency, audio_format, subdir, description, enabled)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            schedule_id,
            data["title"],
            data["stream_url"],
            data["start_time"],
            data["end_time"],
            data.get("frequency", "*"),
            data.get("audio_format", "mp3"),
            data["subdir"],
            data.get("description"),
            1 if data.get("enabled", True) else 0,
        ),
    )
    conn.commit()
    conn.close()
    return get_schedule(schedule_id)  # type: ignore


def update_schedule(schedule_id: str, data: dict[str, Any]) -> Optional[dict[str, Any]]:
    conn = _connect()
    conn.execute(
        """
        UPDATE schedules SET
            title = ?, stream_url = ?, start_time = ?, end_time = ?,
            frequency = ?, audio_format = ?, subdir = ?, description = ?, enabled = ?
        WHERE id = ?
        """,
        (
            data["title"],
            data["stream_url"],
            data["start_time"],
            data["end_time"],
            data.get("frequency", "*"),
            data.get("audio_format", "mp3"),
            data["subdir"],
            data.get("description"),
            1 if data.get("enabled", True) else 0,
            schedule_id,
        ),
    )
    conn.commit()
    conn.close()
    return get_schedule(schedule_id)


def delete_schedule(schedule_id: str) -> bool:
    conn = _connect()
    cur = conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0
