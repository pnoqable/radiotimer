import asyncio

import pytest

import src.playlist as pl


def test_direct_media_passed_through_without_fetch(monkeypatch):
    fetched = []

    class FakeClient:
        def __init__(self, *a, **k):
            fetched.append(True)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            raise AssertionError("should not fetch a direct media URL")

    monkeypatch.setattr(pl.httpx, "AsyncClient", FakeClient)
    out = asyncio.run(pl.resolve_stream_url("https://example.com/ndr_info.mp3"))
    assert out == "https://example.com/ndr_info.mp3"
    # query string must not defeat the extension check
    out2 = asyncio.run(pl.resolve_stream_url("https://example.com/ndr_info.mp3?token=1"))
    assert out2 == "https://example.com/ndr_info.mp3?token=1"
    assert fetched == []


def test_m3u_playlist_is_resolved(monkeypatch):
    class Resp:
        def raise_for_status(self):
            pass

        text = "#EXTM3U\nhttp://real.stream/audio\n"

        async def aiter_text(self):
            yield self.text

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return Resp()

    monkeypatch.setattr(pl.httpx, "AsyncClient", FakeClient)
    out = asyncio.run(pl.resolve_stream_url("https://example.com/play.m3u"))
    assert out == "http://real.stream/audio"


def test_unknown_url_falls_back_without_hanging(monkeypatch):
    # A live stream without a clear extension: we must not download it all.
    class Resp:
        def raise_for_status(self):
            pass

        def __init__(self):
            self._chunks = iter(["\x00\x01" * 100, "\x02\x03" * 100])

        async def aiter_text(self):
            for chunk in self._chunks:
                yield chunk

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return Resp()

    monkeypatch.setattr(pl.httpx, "AsyncClient", FakeClient)
    out = asyncio.run(pl.resolve_stream_url("https://example.com/ndrinfo"))
    assert out == "https://example.com/ndrinfo"
