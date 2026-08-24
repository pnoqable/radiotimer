import asyncio
from typing import Set

# Monotonic counter that is incremented whenever the *structure* of what the
# web UI shows changes (a recording starts/stops, a file is deleted, a schedule
# is edited). Clients subscribe to a Server-Sent-Events stream and only
# re-fetch the full tree on a "state" message, so we never poll on a fixed
# interval.
#
# While a recording is running we additionally push "progress" messages (just
# the file's relative path + current byte size) once per second. That is a true
# delta: the client updates a single DOM node, with no filesystem walk and no
# full re-render.
_state_version = 0

# One asyncio.Queue per connected SSE client. bump()/push_progress() enqueue a
# message into every queue. Using a queue (instead of clearing an Event) avoids
# the race where a notification arrives between the version check and clearing
# the wait primitive.
_subscribers: Set[asyncio.Queue] = set()


def version() -> int:
    return _state_version


def _broadcast(msg) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for q in list(_subscribers):
        loop.call_soon_threadsafe(q.put_nowait, msg)


def bump() -> None:
    """Notify clients that the visible structure changed (full re-fetch)."""
    global _state_version
    _state_version += 1
    _broadcast(("state",))


def push_progress(rel_path: str, size: int) -> None:
    """Notify clients of a live recording's current byte size (delta update)."""
    _broadcast(("progress", rel_path, size))


def subscribe() -> asyncio.Queue:
    q = asyncio.Queue()
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)
