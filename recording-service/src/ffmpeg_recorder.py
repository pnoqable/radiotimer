import asyncio
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Active ffmpeg processes, keyed by an arbitrary run id (e.g. task/schedule id),
# so the web UI / API can report and stop running recordings.
_active: dict[str, asyncio.subprocess.Process] = {}

# Output file paths of active recordings, keyed by the same run id. Used to
# recognise which recordings are still being written (for live/timeshift
# playback of a running recording).
_paths: dict[str, Path] = {}


async def record(
    run_id: str,
    url: str,
    duration_seconds: float,
    output_path: Path,
    stop_event: Optional[asyncio.Event] = None,
    ffmpeg_bin: str = "ffmpeg",
) -> None:
    """Record a stream with ffmpeg for the given duration.

    ffmpeg is asked to reconnect on dropouts and to stop cleanly after the
    duration. If stop_event is set, the process is terminated early via SIGTERM
    (ffmpeg then finalises the file instead of being killed hard).

    ``ffmpeg_bin`` is the ffmpeg executable (overridable for testing).
    """
    cmd = [
        ffmpeg_bin,
        "-y",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "5",
        # Some stream servers only serve clients with a real User-Agent and
        # reject ffmpeg's default "Lavf/..." UA (the browser plays fine, but
        # recording fails with 403). Send a browser-like UA.
        "-user_agent",
        "Mozilla/5.0 (radiotimer)",
        "-i",
        str(url),
        "-t",
        str(int(duration_seconds)),
        "-c",
        "copy",
        str(output_path),
    ]

    logger.info("Starting ffmpeg: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _active[run_id] = proc
    _paths[run_id] = output_path

    try:
        if stop_event is not None:
            await _wait_with_stop(proc, stop_event)
        else:
            await proc.wait()

        if proc.returncode not in (0, None) and (
            stop_event is None or not stop_event.is_set()
        ):
            stderr = await proc.stderr.read() if proc.stderr else b""
            raise RuntimeError(
                f"ffmpeg exited with code {proc.returncode}: {stderr.decode(errors='replace')[:500]}"
            )
    finally:
        _active.pop(run_id, None)
        _paths.pop(run_id, None)


async def _wait_with_stop(
    proc: asyncio.subprocess.Process, stop_event: asyncio.Event
) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=None)
    except asyncio.TimeoutError:
        pass
    if proc.returncode is None:
        # Ask ffmpeg to finalise and exit cleanly (SIGTERM), not SIGKILL.
        proc.terminate()
        try:
            await proc.wait()
        except Exception:
            pass


def is_active(run_id: str) -> bool:
    proc = _active.get(run_id)
    return proc is not None and proc.returncode is None


def stop(run_id: str) -> bool:
    proc = _active.get(run_id)
    if proc is not None and proc.returncode is None:
        proc.terminate()
        return True
    return False


def is_live_path(path: Path) -> bool:
    """Return True if ``path`` is currently being written by a running recording."""
    target = Path(path).resolve()
    return any(Path(p).resolve() == target for p in _paths.values())


async def iter_live_file(path: Path, chunk_size: int = 64 * 1024):
    """Stream a (possibly still growing) recording file to the client.

    Behaves like a radio stream: starts at the beginning and keeps following
    the file as new bytes are written, so the listener hears the recording
    time-shifted (from its start) and catches up to "now" without hitting the
    virtual end that a static file server would report.

    Yields until the recording has finished and the final bytes have been
    flushed, then stops.
    """
    path = Path(path)
    # The writer may not have created the file yet; wait briefly for it.
    waited = 0.0
    while not path.exists():
        if not is_live_path(path):
            # Recording ended before the file ever appeared.
            return
        await asyncio.sleep(0.3)
        waited += 0.3
        if waited > 30:
            return

    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if chunk:
                yield chunk
            elif is_live_path(path):
                # At EOF but still recording: wait for more data.
                await asyncio.sleep(1)
            else:
                # Recording finished: give ffmpeg a moment to flush the last
                # bytes, then stream them and stop.
                await asyncio.sleep(1)
                tail = f.read()
                if tail:
                    yield tail
                break
