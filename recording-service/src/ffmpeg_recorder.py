import asyncio
import logging
from pathlib import Path
from typing import Optional

from src import settings
from src import signals

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
    ffmpeg_bin: str = "ffmpeg",
) -> None:
    """Record a stream with ffmpeg for the given duration.

    ffmpeg is asked to reconnect on dropouts and to stop cleanly after the
    duration. A running recording can be stopped via :func:`stop`, which sends
    SIGTERM so ffmpeg finalises the file instead of being killed hard.

    ``ffmpeg_bin`` is the ffmpeg executable (overridable for testing).
    """
    # Choose the codec. By default we copy (CPU-cheap). When re-encoding is
    # enabled (settings.REENCODE), transcode to a real codec so the recording
    # starts on a clean frame and the MP3 first-frame "click" is avoided -- at
    # the cost of higher CPU load.
    ext = output_path.suffix.lower().lstrip(".")
    if settings.REENCODE:
        codec = {"mp3": "libmp3lame", "aac": "aac", "m4a": "aac", "ogg": "libopus"}.get(ext)
        codec_args = (
            ["-c:a", codec, "-b:a", settings.REENCODE_BITRATE]
            if codec
            else ["-c", "copy"]
        )
    else:
        codec_args = ["-c", "copy"]

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
        # Drop incomplete/corrupt leading packets (e.g. a partial audio frame
        # received when the TCP connection to the live stream opens mid-frame).
        # Without this, every recording starts with a short "click": the very
        # first, truncated frame is written to the file and the decoder glitches
        # on it. This runs purely in the demuxer and does NOT re-encode, so it
        # adds no CPU load (important for low-power hosts like a Raspberry Pi).
        "-fflags",
        "+discardcorrupt",
        "-err_detect",
        "ignore_err",
        "-i",
        str(url),
        "-t",
        str(int(duration_seconds)),
        *codec_args,
        # Flush every packet to disk immediately instead of buffering. This
        # keeps the recorded file growing in small, frequent chunks so a
        # timeshift listener following the file hears "now" with low latency.
        "-flush_packets",
        "1",
        str(output_path),
    ]

    logger.info("Starting ffmpeg: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _active[run_id] = proc
    _paths[run_id] = output_path
    signals.bump()

    drain = asyncio.ensure_future(_drain_stderr(proc.stderr))

    try:
        await proc.wait()

        # A recording stopped via :func:`stop` (SIGTERM) exits with code -15;
        # treat that as a normal stop, not a failure.
        if proc.returncode not in (0, None, -15):
            logger.error(
                "Recording %s failed: ffmpeg exited with code %s",
                run_id, proc.returncode,
            )
            try:
                stderr_data = await asyncio.wait_for(drain, timeout=2.0)
                for line in stderr_data.decode("utf-8", errors="replace").splitlines():
                    logger.error("  ffmpeg: %s", line)
            except (asyncio.TimeoutError, Exception):
                pass
            raise RuntimeError(f"ffmpeg exited with code {proc.returncode}")
    finally:
        drain.cancel()
        _active.pop(run_id, None)
        _paths.pop(run_id, None)
        signals.bump()


async def _drain_stderr(stream) -> bytes:
    """Read ffmpeg's stderr into memory and return it."""
    if stream is None:
        return b""
    buf = bytearray()
    try:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            buf += chunk
    except (asyncio.CancelledError, Exception):
        pass
    return bytes(buf)


def is_active(run_id: str) -> bool:
    proc = _active.get(run_id)
    return proc is not None and proc.returncode is None


def stop(run_id: str) -> bool:
    proc = _active.get(run_id)
    if proc is not None and proc.returncode is None:
        proc.terminate()
        signals.bump()
        return True
    return False


def is_any_active() -> bool:
    return any(is_active(rid) for rid in _active)


def active_recordings():
    """Return ``(run_id, output_path)`` pairs for all running recordings."""
    return list(_paths.items())


def is_live_path(path: Path) -> bool:
    """Return True if ``path`` is currently being written by a running recording."""
    target = Path(path).resolve()
    return any(Path(p).resolve() == target for p in _paths.values())


def get_live_path(run_id: str) -> Optional[Path]:
    """Return the file path currently being written for ``run_id``, if any."""
    return _paths.get(run_id)


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
