import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import src.config  # noqa: ensure package imports work
from src import utils
from src.audio_storage import AudioStorageAdapter
from src.db import (
    create_schedule,
    delete_schedule,
    get_schedule,
    init_db,
    list_schedules,
    update_schedule,
)
from src.ffmpeg_recorder import is_active, stop
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
    for key in ("title", "stream_url", "start_time", "end_time", "subdir"):
        if not payload.get(key):
            raise HTTPException(status_code=400, detail=f"Missing field: {key}")
    created = create_schedule(payload)
    reload_job(created["id"])
    return created


@app.put("/api/schedules/{schedule_id}")
def api_update_schedule(schedule_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not get_schedule(schedule_id):
        raise HTTPException(status_code=404, detail="Not found")
    updated = update_schedule(schedule_id, payload)
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
    jobs = []
    for job in scheduler_service.scheduler.get_jobs():
        jobs.append(
            {
                "id": job.id,
                "name": job.name,
                "next_run": str(job.next_run_time),
                "running": is_active(job.id),
            }
        )
    return {"jobs": jobs}


@app.get("/api/recordings")
def api_recordings() -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    if OUTPUT_DIR.exists():
        for path in OUTPUT_DIR.rglob("*"):
            if path.is_file() and path.suffix.lower() in (
                ".mp3",
                ".mp4",
                ".m4a",
                ".ogg",
            ):
                rel = path.relative_to(OUTPUT_DIR).as_posix()
                results.append(
                    {
                        "path": rel,
                        "name": path.name,
                        "size": path.stat().st_size,
                        "url": f"/files/{rel}",
                    }
                )
    return {"recordings": results}


@app.post("/api/recordings/{schedule_id}/stop")
def api_stop(schedule_id: str) -> dict[str, bool]:
    return {"stopped": stop(schedule_id)}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/files", StaticFiles(directory=OUTPUT_DIR), name="files")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
