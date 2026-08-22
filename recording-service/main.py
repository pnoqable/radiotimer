import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import src.config  # noqa: ensure package imports work
from src import utils
from src.audio_storage import AudioStorageAdapter
from src.db import (
    create_schedule,
    create_station,
    delete_schedule,
    delete_station,
    get_schedule,
    get_station,
    init_db,
    list_schedules,
    list_stations,
    update_schedule,
    update_station,
)
from src.ffmpeg_recorder import is_active, stop
from src.playlist import resolve_stream_url
from src.recording_service import RecordAudioService
from src.schedule_builder import build_schedule
from src.scheduler_service import RecordingSchedulerService
from src.settings import OUTPUT_DIR

logger = logging.getLogger(__name__)

recorder = RecordAudioService(AudioStorageAdapter(), utils.TimeProvider())
scheduler_service = RecordingSchedulerService(recorder, utils.TimeProvider(), "mp3")

STATIC_DIR = Path(__file__).parent / "static"

# Ensure the recordings directory exists before mounting it as static files
# (StaticFiles checks existence at import time, before the lifespan runs).
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

_AUDIO_EXTS = (".mp3", ".mp4", ".m4a", ".ogg")


def reload_job(schedule_id: str) -> None:
    try:
        scheduler_service.scheduler.remove_job(schedule_id)
    except Exception:
        pass
    row = get_schedule(schedule_id)
    if row and row["enabled"]:
        try:
            scheduler_service.add_recording_schedule(build_schedule(row))
        except Exception:
            logger.exception("Failed to reload schedule %s", schedule_id)


def load_all_schedules() -> None:
    for row in list_schedules():
        if not row["enabled"]:
            continue
        try:
            scheduler_service.add_recording_schedule(build_schedule(row))
        except Exception:
            logger.exception("Failed to load schedule %s", row.get("id"))


def _build_recordings_tree(root: Path) -> dict[str, Any]:
    node: dict[str, Any] = {"name": root.name, "type": "folder", "children": []}
    for entry in sorted(os.listdir(root)):
        path = root / entry
        if path.is_dir():
            node["children"].append(_build_recordings_tree(path))
        elif path.suffix.lower() in _AUDIO_EXTS:
            rel = path.relative_to(OUTPUT_DIR).as_posix()
            node["children"].append(
                {
                    "name": path.name,
                    "type": "file",
                    "path": rel,
                    "size": path.stat().st_size,
                    "url": f"/files/{rel}",
                }
            )
    node["children"].sort(key=lambda c: c["name"].lower())
    return node


@asynccontextmanager
async def lifespan(app: FastAPI):
    utils.setup_logging(logging.INFO)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    load_all_schedules()
    scheduler_service.run()
    yield
    try:
        scheduler_service.scheduler.remove_all_jobs()
        scheduler_service.scheduler.shutdown(wait=False)
    except Exception:
        pass


app = FastAPI(title="radiotimer", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Stations
# ---------------------------------------------------------------------------


@app.get("/api/stations")
def api_list_stations() -> list[dict[str, Any]]:
    return list_stations()


@app.get("/api/stations/{station_id}")
def api_get_station(station_id: str) -> dict[str, Any]:
    station = get_station(station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Not found")
    return station


@app.post("/api/stations")
def api_create_station(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return create_station(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/stations/{station_id}")
def api_update_station(station_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not get_station(station_id):
        raise HTTPException(status_code=404, detail="Not found")
    try:
        updated = update_station(station_id, payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return updated  # type: ignore


@app.delete("/api/stations/{station_id}")
def api_delete_station(station_id: str) -> dict[str, bool]:
    try:
        if not delete_station(station_id):
            raise HTTPException(status_code=404, detail="Not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.get("/api/stations/{station_id}/resolve")
async def api_resolve_station(station_id: str) -> dict[str, Any]:
    # Resolve the (possibly .m3u/.pls) station URL to the direct stream URL so
    # it can be tested/played directly in a browser <audio> element.
    station = get_station(station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        url = await resolve_stream_url(station["url"])
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Konnte Stream-URL nicht auflösen: {e}")
    return {"url": url}


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


@app.get("/api/schedules")
def api_list_schedules() -> list[dict[str, Any]]:
    return list_schedules()


@app.get("/api/schedules/{schedule_id}")
def api_get_schedule(schedule_id: str) -> dict[str, Any]:
    schedule = get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Not found")
    return schedule


@app.post("/api/schedules")
def api_create_schedule(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        created = create_schedule(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    reload_job(created["id"])
    return created


@app.put("/api/schedules/{schedule_id}")
def api_update_schedule(schedule_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not get_schedule(schedule_id):
        raise HTTPException(status_code=404, detail="Not found")
    try:
        updated = update_schedule(schedule_id, payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    reload_job(schedule_id)
    return updated  # type: ignore


@app.delete("/api/schedules/{schedule_id}")
def api_delete_schedule(schedule_id: str) -> dict[str, bool]:
    if not delete_schedule(schedule_id):
        raise HTTPException(status_code=404, detail="Not found")
    try:
        scheduler_service.scheduler.remove_job(schedule_id)
    except Exception:
        pass
    return {"ok": True}


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    now = utils.TimeProvider().get_current_time()
    jobs = []
    for job in scheduler_service.scheduler.get_jobs():
        due = False
        row = get_schedule(job.id)
        if row and row["enabled"]:
            try:
                period = build_schedule(row).resolve_recording_period(now)
                due = period.start <= now <= period.end
            except Exception:
                due = False
        jobs.append(
            {
                "id": job.id,
                "name": job.name,
                "next_run": str(job.next_run_time),
                "running": is_active(job.id),
                "due": due,
            }
        )
    return {"jobs": jobs}


@app.get("/api/recordings")
def api_recordings() -> dict[str, Any]:
    if not OUTPUT_DIR.exists():
        return {"tree": {"name": OUTPUT_DIR.name, "type": "folder", "children": []}}
    tree = _build_recordings_tree(OUTPUT_DIR)
    return {"tree": tree}


@app.delete("/api/recordings")
def api_delete_recording(path: str = Query(...)) -> dict[str, bool]:
    base = OUTPUT_DIR.resolve()
    target = (base / path).resolve()
    # Prevent path traversal: the resolved target must stay inside OUTPUT_DIR.
    if target != base and base not in target.parents:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    target.unlink()
    _prune_empty_dirs(target.parent, base)
    return {"ok": True}


def _prune_empty_dirs(directory: Path, base: Path) -> None:
    """Remove ``directory`` and any now-empty ancestors up to (but not including) ``base``."""
    current = directory.resolve()
    base_resolved = base.resolve()
    while current != base_resolved and base_resolved in current.parents:
        if not any(current.iterdir()):
            current.rmdir()
            current = current.parent
        else:
            break


@app.post("/api/recordings/{schedule_id}/stop")
def api_stop(schedule_id: str) -> dict[str, bool]:
    return {"stopped": stop(schedule_id)}


@app.post("/api/recordings/{schedule_id}/start")
def api_start(schedule_id: str) -> dict[str, Any]:
    row = get_schedule(schedule_id)
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    if not row["enabled"]:
        raise HTTPException(status_code=400, detail="Schedule is disabled")
    now = utils.TimeProvider().get_current_time()
    period = build_schedule(row).resolve_recording_period(now)
    if not (period.start <= now <= period.end):
        raise HTTPException(
            status_code=400, detail="Not currently within the recording window"
        )
    if is_active(schedule_id):
        return {"started": False, "reason": "already running"}
    # Re-fire the existing cron job immediately; subsequent runs follow the
    # normal schedule. If the job is somehow missing, recreate it first.
    try:
        scheduler_service.scheduler.modify_job(
            schedule_id, next_run_time=datetime.now(timezone.utc)
        )
    except Exception:
        reload_job(schedule_id)
        scheduler_service.scheduler.modify_job(
            schedule_id, next_run_time=datetime.now(timezone.utc)
        )
    return {"started": True}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/files", StaticFiles(directory=OUTPUT_DIR), name="files")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
