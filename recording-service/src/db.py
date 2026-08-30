import sqlite3
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from src import settings


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def init_db() -> None:
    conn = _connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stations (
            id       TEXT PRIMARY KEY,
            name     TEXT NOT NULL,
            url      TEXT NOT NULL,
            enabled  INTEGER NOT NULL DEFAULT 1
        )
        """
    )

    # Detect whether an existing schedules table uses the OLD schema
    # (stream_url stored directly on the schedule).
    cols = [c[1] for c in conn.execute("PRAGMA table_info(schedules)").fetchall()]
    if "stream_url" in cols:
        conn.execute("ALTER TABLE schedules RENAME TO schedules_old")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schedules (
            id           TEXT PRIMARY KEY,
            title        TEXT NOT NULL,
            station_id   TEXT NOT NULL,
            start_time   TEXT NOT NULL,
            end_time     TEXT NOT NULL,
            frequency    TEXT NOT NULL DEFAULT '*',
            audio_format TEXT NOT NULL DEFAULT 'mp3',
            enabled      INTEGER NOT NULL DEFAULT 1,
            one_off      INTEGER NOT NULL DEFAULT 0,
            start_date   TEXT,
            FOREIGN KEY (station_id) REFERENCES stations(id)
        )
        """
    )

    # One-time migration: add the one_off / start_date columns needed for
    # single (date-bound) recordings. Existing rows default to recurring.
    cols = [c[1] for c in conn.execute("PRAGMA table_info(schedules)").fetchall()]
    if "one_off" not in cols:
        conn.execute("ALTER TABLE schedules ADD COLUMN one_off INTEGER NOT NULL DEFAULT 0")
    if "start_date" not in cols:
        conn.execute("ALTER TABLE schedules ADD COLUMN start_date TEXT")

    # One-time migration: the old schema stored the stream URL directly on the
    # schedule. Move those into stations and reference them instead. Also covers
    # a previously interrupted migration (data left in schedules_old).
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "schedules_old" in tables:
        _migrate_from_old(conn)
        conn.execute("DROP TABLE schedules_old")

    conn.commit()
    conn.close()


def _migrate_from_old(conn: sqlite3.Connection) -> None:
    logger = __import__("logging").getLogger(__name__)
    logger.info("Migrating old schedules (stream_url -> stations)")
    rows = conn.execute("SELECT * FROM schedules_old").fetchall()

    station_by_url: dict[str, str] = {}
    for row in rows:
        url = _row_get(row, "stream_url")
        if url not in station_by_url:
            station_id = str(uuid.uuid4())
            host = urlparse(url).netloc or url
            conn.execute(
                "INSERT INTO stations (id, name, url, enabled) VALUES (?, ?, ?, 1)",
                (station_id, host, url),
            )
            station_by_url[url] = station_id

    for row in rows:
        conn.execute(
            """
            INSERT INTO schedules
                (id, title, station_id, start_time, end_time, frequency, audio_format, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _row_get(row, "id"),
                _row_get(row, "title"),
                station_by_url[_row_get(row, "stream_url")],
                _row_get(row, "start_time"),
                _row_get(row, "end_time"),
                _row_get(row, "frequency", "*"),
                _row_get(row, "audio_format", "mp3"),
                _row_get(row, "enabled", 1),
            ),
        )


# ---------------------------------------------------------------------------
# Stations
# ---------------------------------------------------------------------------


def _station_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "url": row["url"],
        "enabled": bool(row["enabled"]),
    }


def list_stations() -> list[dict[str, Any]]:
    conn = _connect()
    rows = conn.execute("SELECT * FROM stations ORDER BY LOWER(name)").fetchall()
    conn.close()
    return [_station_row_to_dict(r) for r in rows]


def get_station(station_id: str) -> Optional[dict[str, Any]]:
    conn = _connect()
    row = conn.execute("SELECT * FROM stations WHERE id = ?", (station_id,)).fetchone()
    conn.close()
    return _station_row_to_dict(row) if row else None


def create_station(data: dict[str, Any]) -> dict[str, Any]:
    if not data.get("name") or not data.get("url"):
        raise ValueError("Station requires 'name' and 'url'")
    station_id = data.get("id") or str(uuid.uuid4())
    conn = _connect()
    conn.execute(
        "INSERT INTO stations (id, name, url, enabled) VALUES (?, ?, ?, ?)",
        (
            station_id,
            data["name"],
            data["url"],
            1 if data.get("enabled", True) else 0,
        ),
    )
    conn.commit()
    conn.close()
    return get_station(station_id)  # type: ignore


def update_station(station_id: str, data: dict[str, Any]) -> Optional[dict[str, Any]]:
    if not get_station(station_id):
        return None
    conn = _connect()
    conn.execute(
        "UPDATE stations SET name = ?, url = ?, enabled = ? WHERE id = ?",
        (
            data["name"],
            data["url"],
            1 if data.get("enabled", True) else 0,
            station_id,
        ),
    )
    conn.commit()
    conn.close()
    return get_station(station_id)


def delete_station(station_id: str) -> bool:
    conn = _connect()
    count = conn.execute(
        "SELECT COUNT(*) FROM schedules WHERE station_id = ?", (station_id,)
    ).fetchone()[0]
    if count > 0:
        conn.close()
        raise ValueError("Station is still used by schedules and cannot be deleted")
    cur = conn.execute("DELETE FROM stations WHERE id = ?", (station_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


def _schedule_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "station_id": row["station_id"],
        "station_name": row["station_name"],
        "station_url": row["station_url"],
        "start_time": row["start_time"],
        "end_time": row["end_time"],
        "frequency": row["frequency"],
        "audio_format": row["audio_format"],
        "enabled": bool(row["enabled"]),
        "one_off": bool(row["one_off"]),
        "start_date": row["start_date"],
    }


def _select_schedules(where_clause: str = "", params: tuple = ()) -> list[dict[str, Any]]:
    sql = """
        SELECT s.*, st.name AS station_name, st.url AS station_url
        FROM schedules s
        JOIN stations st ON st.id = s.station_id
    """
    if where_clause:
        sql += " WHERE " + where_clause
    sql += " ORDER BY LOWER(s.title)"
    conn = _connect()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [_schedule_row_to_dict(r) for r in rows]


def list_schedules() -> list[dict[str, Any]]:
    return _select_schedules()


def get_schedule(schedule_id: str) -> Optional[dict[str, Any]]:
    rows = _select_schedules("s.id = ?", (schedule_id,))
    return rows[0] if rows else None


def create_schedule(data: dict[str, Any]) -> dict[str, Any]:
    for key in ("title", "station_id", "start_time", "end_time"):
        if not data.get(key):
            raise ValueError(f"Missing field: {key}")

    station = get_station(data["station_id"])
    if station is None:
        raise ValueError(f"Unknown station_id: {data['station_id']}")

    schedule_id = data.get("id") or str(uuid.uuid4())
    conn = _connect()
    conn.execute(
        """
        INSERT INTO schedules
            (id, title, station_id, start_time, end_time, frequency, audio_format, enabled, one_off, start_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            schedule_id,
            data["title"],
            data["station_id"],
            data["start_time"],
            data["end_time"],
            data.get("frequency", "*"),
            data.get("audio_format", "mp3"),
            1 if data.get("enabled", True) else 0,
            1 if data.get("one_off", False) else 0,
            data.get("start_date"),
        ),
    )
    conn.commit()
    conn.close()
    return get_schedule(schedule_id)  # type: ignore


def update_schedule(schedule_id: str, data: dict[str, Any]) -> Optional[dict[str, Any]]:
    existing = get_schedule(schedule_id)
    if not existing:
        return None
    if data.get("station_id"):
        station = get_station(data["station_id"])
        if station is None:
            raise ValueError(f"Unknown station_id: {data['station_id']}")

    conn = _connect()
    conn.execute(
        """
        UPDATE schedules SET
            title = ?, station_id = ?, start_time = ?, end_time = ?,
            frequency = ?, audio_format = ?, enabled = ?, one_off = ?, start_date = ?
        WHERE id = ?
        """,
        (
            data["title"],
            data["station_id"],
            data["start_time"],
            data["end_time"],
            data.get("frequency", "*"),
            data.get("audio_format", "mp3"),
            1 if data.get("enabled", existing["enabled"]) else 0,
            1 if data.get("one_off", bool(existing["one_off"])) else 0,
            data.get("start_date", existing["start_date"]),
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
