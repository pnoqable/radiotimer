import asyncio
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Active ffmpeg processes, keyed by an arbitrary run id (e.g. task/schedule id),
# so the web UI / API can report and stop running recordings.
_active: dict[str, asyncio.subprocess.Process] = {}


async def record(
    run_id: str,
    url: str,
    duration_seconds: float,
    output_path: Path,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """Record a stream with ffmpeg for the given duration.

    ffmpeg is asked to reconnect on dropouts and to stop cleanly after the
    duration. If stop_event is set, the process is terminated early via SIGTERM
    (ffmpeg then finalises the file instead of being killed hard).
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "5",
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


def is_active(run_id: str) -> bool:
    proc = _active.get(run_id)
    return proc is not None and proc.returncode is None


def stop(run_id: str) -> bool:
    proc = _active.get(run_id)
    if proc is not None and proc.returncode is None:
        proc.terminate()
        return True
    return False
