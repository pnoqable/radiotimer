import logging
import mimetypes
import os
import re
import urllib.parse
import xml.sax.saxutils as _xml_escape
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
import pendulum
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
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
from src.ffmpeg_recorder import is_active, is_live_path, iter_live_file, get_live_path, stop
from src.playlist import resolve_stream_url
from src.recording_service import RecordAudioService
from src.schedule_builder import build_schedule
from src.scheduler_service import RecordingSchedulerService
from src import settings

logger = logging.getLogger(__name__)

recorder = RecordAudioService(AudioStorageAdapter(), utils.TimeProvider())
scheduler_service = RecordingSchedulerService(recorder, utils.TimeProvider(), "mp3")

STATIC_DIR = Path(__file__).parent / "static"

# Ensure the recordings directory exists before mounting it as static files
# (StaticFiles checks existence at import time, before the lifespan runs).
settings.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

_AUDIO_EXTS = (".mp3", ".mp4", ".m4a", ".ogg")


def reload_job(schedule_id: str) -> None:
    try:
        scheduler_service.scheduler.remove_job(schedule_id)
    except Exception:
        pass
    row = get_schedule(schedule_id)
    if row and row["enabled"]:
        schedule = build_schedule(row)
        try:
            scheduler_service.add_recording_schedule(schedule)
        except Exception:
            logger.exception("Failed to reload schedule %s", schedule_id)
        # If a recording is currently running but the edited schedule no longer
        # covers "now", stop it (e.g. the time window was shortened or moved).
        now = utils.TimeProvider().get_current_time()
        period = schedule.resolve_recording_period(now)
        if is_active(schedule_id) and not (period.start <= now < period.end):
            stop(schedule_id)
    else:
        # A disabled (or deleted) schedule must not keep recording: stop any
        # in-progress run for it. Re-enabling a schedule that is currently
        # within its window will start recording again automatically.
        stop(schedule_id)


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
            rel = path.relative_to(settings.OUTPUT_DIR).as_posix()
            node["children"].append(
                {
                    "name": path.name,
                    "type": "file",
                    "path": rel,
            "size": path.stat().st_size,
            "url": f"/api/recordings/live?path={urllib.parse.quote(rel)}",
            "live": is_live_path(path),
                }
            )
    node["children"].sort(key=lambda c: c["name"].lower())
    return node


_FILE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ _](\d{2})-(\d{2})")


def _pubdate_for_file(path: Path) -> datetime:
    """Best-effort publication date for a recording file.

    Recording files are named "<YYYY>-<MM>-<DD> <HH>-<MM>.<ext>", which carries
    the start time. Fall back to the file mtime if the name does not match.
    """
    tz = pendulum.timezone(settings.TIME_ZONE)
    m = _FILE_RE.match(path.stem)
    if m:
        try:
            return pendulum.datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4)), int(m.group(5)), tz=tz,
            )
        except Exception:
            pass
    return pendulum.from_timestamp(path.stat().st_mtime, tz=tz)


def _rfc822(dt: datetime) -> str:
    return dt.strftime("%a, %d %b %Y %H:%M:%S %z")


def _build_podcast_feed(folder_rel: str, request: Request) -> str:
    """Build an RSS 2.0 podcast feed for all audio files under ``folder_rel``.

    The folder may be any directory under OUTPUT_DIR (a station folder, a
    single show folder, or empty for everything). Files are listed recursively
    and sorted newest-first.
    """
    base = settings.OUTPUT_DIR.resolve()
    folder = (base / folder_rel).resolve()
    # Prevent path traversal: the resolved folder must stay inside OUTPUT_DIR.
    if folder != base and base not in folder.parents:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not folder.is_dir():
        raise HTTPException(status_code=404, detail="Not found")

    public = (settings.PUBLIC_URL or str(request.base_url).rstrip("/"))
    feed_path = "/api/podcast?folder=" + urllib.parse.quote(folder_rel)
    feed_url = public + feed_path

    items = []
    for entry in sorted(folder.rglob("*")):
        if not entry.is_file() or entry.suffix.lower() not in _AUDIO_EXTS:
            continue
        rel = entry.relative_to(base).as_posix()
        parts = rel.split("/")
        # Episode title: the file name (e.g. "2026-08-23 18-23") followed by the
        # path components (station, show, ...) in reverse order, so a file at
        # "Bayern 2 Süd/Test/2026-08-23 18-23.mp3" becomes
        # "2026-08-23 18-23 Test Bayern 2 Süd".
        folder_parts = parts[:-1]
        title = entry.stem
        if folder_parts:
            title = title + " " + ", ".join(reversed(folder_parts))
        media_type = mimetypes.guess_type(str(entry))[0] or "application/octet-stream"
        enc_url = public + "/api/recordings/live?path=" + urllib.parse.quote(rel)
        items.append(
            {
                "title": title,
                "enc_url": enc_url,
                "length": entry.stat().st_size,
                "type": media_type,
                "pub": _pubdate_for_file(entry),
            }
        )
    items.sort(key=lambda i: i["pub"], reverse=True)

    title = folder_rel.strip("/").split("/")[-1] or "Aufnahmen"
    esc = _xml_escape.escape
    item_xml = []
    for it in items:
        item_xml.append(
            "    <item>\n"
            f"      <title>{esc(it['title'])}</title>\n"
            f"      <link>{esc(it['enc_url'])}</link>\n"
            f"      <guid isPermaLink=\"false\">{esc(it['enc_url'])}</guid>\n"
            f"      <pubDate>{_rfc822(it['pub'])}</pubDate>\n"
            f"      <enclosure url=\"{esc(it['enc_url'])}\" length=\"{it['length']}\" type=\"{esc(it['type'])}\"/>\n"
            f"      <description>{esc(it['title'])}</description>\n"
            "    </item>"
        )
    items_block = "\n".join(item_xml)

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">\n'
        "  <channel>\n"
        f"    <title>{esc(title)}</title>\n"
        f"    <link>{esc(feed_url)}</link>\n"
        f"    <description>Aufnahmen aus {esc(folder_rel or '/')}</description>\n"
        "    <language>de</language>\n"
        f"    <lastBuildDate>{_rfc822(pendulum.now(pendulum.timezone(settings.TIME_ZONE)))}</lastBuildDate>\n"
        f"{items_block}\n"
        "  </channel>\n"
        "</rss>\n"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    utils.setup_logging(logging.INFO)
    settings.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
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


@app.get("/api/stations/{station_id}/open")
async def api_open_station(station_id: str) -> RedirectResponse:
    # Resolve the (possibly .m3u/.pls) station URL to the direct stream URL and
    # redirect to it, so a small popup window can play the stream directly.
    station = get_station(station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        url = await resolve_stream_url(station["url"])
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Konnte Stream-URL nicht auflösen: {e}")
    return RedirectResponse(url=url, status_code=307)


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
    stop(schedule_id)
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
        live_url = None
        if is_active(job.id):
            lp = get_live_path(job.id)
            if lp is not None:
                try:
                    rel = lp.resolve().relative_to(settings.OUTPUT_DIR.resolve()).as_posix()
                    live_url = f"/api/recordings/live?path={urllib.parse.quote(rel)}"
                except Exception:
                    live_url = None
        jobs.append(
            {
                "id": job.id,
                "name": job.name,
                "next_run": str(job.next_run_time),
                "running": is_active(job.id),
                "due": due,
                "live_url": live_url,
            }
        )
    return {"jobs": jobs}


@app.get("/api/recordings")
def api_recordings() -> dict[str, Any]:
    if not settings.OUTPUT_DIR.exists():
        return {"tree": {"name": settings.OUTPUT_DIR.name, "type": "folder", "children": []}}
    tree = _build_recordings_tree(settings.OUTPUT_DIR)
    return {"tree": tree}


@app.delete("/api/recordings")
def api_delete_recording(path: str = Query(...)) -> dict[str, bool]:
    base = settings.OUTPUT_DIR.resolve()
    target = (base / path).resolve()
    # Prevent path traversal: the resolved target must stay inside settings.OUTPUT_DIR.
    if target != base and base not in target.parents:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    target.unlink()
    _prune_empty_dirs(target.parent, base)
    return {"ok": True}


@app.get("/api/recordings/live")
def api_recordings_live(path: str = Query(...)):
    """Stream a recording file.

    If the file is currently being recorded, it is served as a live
    (timeshift) stream that follows the growing file, so the listener starts
    at the beginning and catches up to "now". Finished files are served
    statically.
    """
    base = settings.OUTPUT_DIR.resolve()
    target = (base / path).resolve()
    # Prevent path traversal: the resolved target must stay inside settings.OUTPUT_DIR.
    if target != base and base not in target.parents:
        raise HTTPException(status_code=400, detail="Invalid path")

    if is_live_path(target):
        import mimetypes

        media_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        return StreamingResponse(
            iter_live_file(target),
            media_type=media_type,
        )

    if target.is_file():
        return FileResponse(target)
    raise HTTPException(status_code=404, detail="Not found")


@app.get("/api/podcast")
def api_podcast(request: Request, folder: str = Query("")):
    """Return an RSS 2.0 podcast feed for the given recordings folder.

    ``folder`` is a path relative to OUTPUT_DIR and may point at a station
    folder, a single show folder, or be empty to cover all recordings. The feed
    lists every audio file below it (recursively), newest first.
    """
    xml = _build_podcast_feed(folder, request)
    return Response(content=xml, media_type="application/rss+xml")


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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/files", StaticFiles(directory=settings.OUTPUT_DIR), name="files")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
