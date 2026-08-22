import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# File extensions that ffmpeg can consume directly as a stream input.
_HLS_EXT = ".m3u8"


async def resolve_stream_url(url: str) -> str:
    """Return a stream URL ffmpeg can record from.

    The station URL maintained by the user may be a plain Shoutcast/Icecast
    ``.m3u`` (or ``.pls``) playlist that only points at the real stream. ffmpeg
    cannot parse those, so we fetch and extract the first real ``http(s)`` URL.
    HLS playlists (``.m3u8``) and direct streams are passed through unchanged.

    The playlist is resolved fresh on every call (no caching) so station URL
    changes are picked up at the next recording.
    """
    lowered = url.lower()
    if lowered.endswith(_HLS_EXT):
        return url

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            text = resp.text
    except Exception as exc:  # noqa: BLE001 - fall back to the raw URL
        logger.warning("Could not fetch playlist %s: %s", url, exc)
        return url

    extracted = _extract_stream_url(text)
    if extracted:
        return extracted
    logger.warning("No stream URL found in playlist %s, using raw URL", url)
    return url


def _extract_stream_url(text: str) -> Optional[str]:
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # .pls style: File1=http://...
        if line.lower().startswith("file") and "=" in line:
            value = line.split("=", 1)[1].strip()
            if value.startswith("http"):
                return value
            continue
        # comments / headers in .m3u
        if line.startswith("#"):
            continue
        if line.startswith("http"):
            return line
    return None
