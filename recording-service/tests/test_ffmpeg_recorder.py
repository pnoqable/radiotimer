import asyncio
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from src import ffmpeg_recorder

STUB = textwrap.dedent(
    r"""
    #!/usr/bin/env python3
    # Minimal fake ffmpeg for tests: create the output file (last argument)
    # and exit. If "-t <sec>" with sec > 5 is given, sleep that long first
    # so the stop/terminate path can be exercised. Unlike a shell script, a
    # Python process terminates immediately on SIGTERM in every environment.
    import sys
    import time

    args = sys.argv[1:]
    prev = None
    t = None
    out = None
    for a in args:
        if prev == "-t":
            t = a
        out = a
        prev = a

    if t and float(t) > 5:
        time.sleep(float(t))

    with open(out, "w"):
        pass
    sys.exit(0)
    """
).lstrip("\n")


@pytest.fixture
def stub_ffmpeg():
    script = Path(__file__).parent / "fake_ffmpeg"
    script.write_text(STUB)
    script.chmod(
        script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
    )
    try:
        yield str(script)
    finally:
        script.unlink()


async def test_record_creates_output(stub_ffmpeg, tmp_path):
    out = tmp_path / "rec.mp3"
    await ffmpeg_recorder.record(
        run_id="job1",
        url="http://example.com/stream.mp3",
        duration_seconds=1,
        output_path=out,
        ffmpeg_bin=stub_ffmpeg,
    )
    assert out.exists()
    assert not ffmpeg_recorder.is_active("job1")


async def test_record_command_includes_user_agent(monkeypatch):
    captured = {}

    class FakeProc:
        returncode = 0
        stderr = None

        async def wait(self):
            return 0

        def terminate(self):
            pass

    async def fake_exec(*args, **kwargs):
        captured["cmd"] = list(args)
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    out = Path("/tmp/rec.mp3")
    await ffmpeg_recorder.record(
        run_id="j-ua",
        url="http://example.com/stream.mp3",
        duration_seconds=1,
        output_path=out,
    )
    assert "-user_agent" in captured["cmd"]
    assert any("Mozilla" in str(a) for a in captured["cmd"])
    assert "-flush_packets" in captured["cmd"]
    # Demuxer flags that help avoid a leading glitch (corrupt/partial leading
    # frames) without re-encoding. They do not fully fix the MP3 first-frame
    # bit-reservoir click, which would require a copy-only re-mux or re-encode.
    assert "+discardcorrupt" in captured["cmd"]
    assert "ignore_err" in captured["cmd"]


async def test_record_command_reenCodes_when_enabled(monkeypatch):
    captured = {}

    class FakeProc:
        returncode = 0
        stderr = None

        async def wait(self):
            return 0

        def terminate(self):
            pass

    async def fake_exec(*args, **kwargs):
        captured["cmd"] = list(args)
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(ffmpeg_recorder.settings, "REENCODE", True)
    out = Path("/tmp/rec.mp3")
    await ffmpeg_recorder.record(
        run_id="j-re",
        url="http://example.com/stream.mp3",
        duration_seconds=1,
        output_path=out,
    )
    # Re-encoding path: a real audio codec + bitrate, and no plain copy.
    assert "-c:a" in captured["cmd"]
    assert "libmp3lame" in captured["cmd"]
    assert "192k" in captured["cmd"]
    assert "copy" not in captured["cmd"]


async def test_record_stop_event_terminates_early(stub_ffmpeg, tmp_path):
    out = tmp_path / "rec.mp3"
    stop = asyncio.Event()

    async def trigger_stop():
        await asyncio.sleep(0.3)
        stop.set()

    task = asyncio.create_task(
        ffmpeg_recorder.record(
            run_id="job2",
            url="http://example.com/stream.mp3",
            duration_seconds=30,  # stub would sleep 30s without a stop
            output_path=out,
            stop_event=stop,
            ffmpeg_bin=stub_ffmpeg,
        )
    )
    asyncio.create_task(trigger_stop())

    # Should finish well before the 30s stub sleep because we terminate it.
    await asyncio.wait_for(task, timeout=5)
    assert not ffmpeg_recorder.is_active("job2")
