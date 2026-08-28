import asyncio
import logging
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


async def test_record_stop_terminates_early_and_is_not_a_failure(monkeypatch, caplog, tmp_path):
    """Stopping a recording via ffmpeg_recorder.stop() (SIGTERM -> exit -15)
    is a normal stop: it must not log an error nor raise RuntimeError, and it
    must clear the live state.
    """

    class FakeProc:
        returncode = -15

        def __init__(self):
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_data(b"")
            self.stderr.feed_eof()
            self._terminated = False

        async def wait(self):
            return -15

        def terminate(self):
            self._terminated = True
            self.returncode = -15

    async def fake_exec(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    out = tmp_path / "rec_stop.mp3"
    with caplog.at_level(logging.ERROR, logger="src.ffmpeg_recorder"):
        await ffmpeg_recorder.record(
            run_id="j-stop",
            url="http://example.com/stream.mp3",
            duration_seconds=30,
            output_path=out,
        )
    # No ERROR records, no raised error: a SIGTERM stop is an ordinary stop.
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)
    assert not ffmpeg_recorder.is_active("j-stop")


async def test_record_captures_stderr_on_failure(monkeypatch, caplog):
    """A non-zero exit must log ffmpeg's stderr and clear the live state."""

    class FakeProc:
        returncode = 1

        def __init__(self):
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_data(b"Connection refused: 403\n")
            self.stderr.feed_eof()

        async def wait(self):
            return 1

        def terminate(self):
            pass

    async def fake_exec(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    out = Path("/tmp/rec_err.mp3")
    with caplog.at_level(logging.ERROR, logger="src.ffmpeg_recorder"), \
            pytest.raises(RuntimeError) as exc:
        await ffmpeg_recorder.record(
            run_id="j-err",
            url="http://example.com/stream.mp3",
            duration_seconds=1,
            output_path=out,
        )
    assert "exited with code 1" in str(exc.value)
    assert not out.exists()
    # No error file may be written into the filesystem anymore.
    error_file = out.with_name(out.name + ".error.txt")
    assert not error_file.exists()
    # The captured stderr must be surfaced through the logger.
    assert any("ffmpeg exited with code 1" in r.message for r in caplog.records)
    assert any("Connection refused: 403" in r.message for r in caplog.records)
    # record() returned, so its finally must have cleared the live state.
    assert not ffmpeg_recorder.is_active("j-err")
    assert not ffmpeg_recorder.is_live_path(out)


async def test_record_writes_no_error_log_on_success(monkeypatch, tmp_path, caplog):
    """A clean exit (returncode 0) must not log an error or leave anything
    behind, even when ffmpeg emits benign stderr output such as progress lines.
    """

    class FakeProc:
        returncode = 0

        def __init__(self):
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_data(b"frame= 100 fps= 25 ...\n")
            self.stderr.feed_eof()

        async def wait(self):
            return 0

        def terminate(self):
            pass

    async def fake_exec(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    out = tmp_path / "rec_ok.mp3"
    await ffmpeg_recorder.record(
        run_id="j-ok",
        url="http://example.com/stream.mp3",
        duration_seconds=1,
        output_path=out,
    )
    error_file = out.with_name(out.name + ".error.txt")
    assert not error_file.exists()
    # A clean exit must not produce any ERROR-level log records either.
    assert not any(
        r.levelno >= logging.ERROR for r in caplog.records
    )
    assert not ffmpeg_recorder.is_active("j-ok")


async def test_record_does_not_hang_when_stderr_never_eofs(monkeypatch):
    """Regression: a stuck stderr pipe (no EOF) must not block record() forever.

    Previously the code did `await proc.stderr.read()` *after* proc.wait(), which
    hung indefinitely when ffmpeg closed without delivering EOF and left the
    recording stuck as "live" in the UI. Now stderr is drained concurrently and
    the error text is read with a timeout, so record() always terminates.
    """

    class FakeProc:
        returncode = 1

        def __init__(self):
            # Deliberately never feed EOF, simulating a stuck pipe.
            self.stderr = asyncio.StreamReader()

        async def wait(self):
            return 1

        def terminate(self):
            pass

    async def fake_exec(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    out = Path("/tmp/rec_hang.mp3")

    # Without the fix this would hang past the 10s outer bound.
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(
            ffmpeg_recorder.record(
                run_id="j-hang",
                url="http://example.com/stream.mp3",
                duration_seconds=1,
                output_path=out,
            ),
            timeout=10,
        )
    assert not ffmpeg_recorder.is_active("j-hang")
    assert not ffmpeg_recorder.is_live_path(out)
