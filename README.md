# radiotimer

Self-hosted web service that records web radio shows on a schedule. Successor
to the old "vlc timer" (Qt/Windows). Built on FastAPI + APScheduler + ffmpeg,
with a small vanilla-JS web UI.

> Forked from [holstt/stream2podcast](https://github.com/holstt/stream2podcast)
> but has since diverged significantly: it is now a single service with a web UI
> and SQLite store, uses ffmpeg for recording, and no longer generates podcast
> feeds as its primary purpose.

## Features

- Schedule recordings of HTTP audio streams (ICY/HLS, `.m3u`/`.pls` playlists
  are resolved automatically) per station.
- Web UI to manage stations and schedules, show live status, and browse
  recordings with player + download.
- Live / timeshift playback of a recording that is still in progress.
- ffmpeg-based capture with reconnect and a clean SIGTERM stop (`-t`).
- SQLite persistence; Server-Sent-Events push UI updates (no polling).
- Optional podcast RSS feed (`/api/podcast`) for phone playback.

## Requirements

- Python 3.11
- `ffmpeg` available on `PATH`

## Quick start (local)

```bash
cd recording-service
pip install -r requirements.txt -r requirements-dev.txt
python main.py            # serves http://0.0.0.0:8000
```

Configuration is via environment variables (see below); no config file is
required.

## Configuration (environment variables)

| Variable               | Default                                 | Purpose                                 |
|------------------------|-----------------------------------------|-----------------------------------------|
| `RADIOTIMER_OUTPUT`    | `./recordings`                          | Where recordings are written            |
| `RADIOTIMER_DB`        | `./app.db`                              | SQLite database path                    |
| `RADIOTIMER_TZ`        | `Europe/Berlin`                         | Time zone for schedules / cron          |
| `RADIOTIMER_PATTERN`   | `{station}/{title}/{date} {start_hm}.{ext}` | Output path pattern                 |
| `RADIOTIMER_REENCODE`  | `false`                                 | Re-encode instead of stream copy        |
| `RADIOTIMER_PUBLIC_URL`| (unset)                                 | Public base URL for podcast enclosures  |

## Deployment

### Docker

```bash
docker compose up -d
```

Builds the image (Debian-slim + ffmpeg), exposes port 8000, mounts `./recordings`
and `./data`, and passes the `RADIOTIMER_*` env vars configured in
`docker-compose.yml`.

### Raspberry Pi (native)

```bash
sudo bash deploy/raspberrypi/install.sh
```

Installs a systemd `radiotimer` service and optionally Samba (SMB share) and an
nginx reverse proxy. Copy `deploy/raspberrypi/.env.example` to `.env` and adjust
paths / time zone before running.

## Using the web UI

Add a **station** (name + stream URL) and one or more **schedules** (start/end
time, weekday frequency, audio format). Active recordings show a green dot; the
running show can be paused and its live stream opened directly from the UI.

## Live playback & podcast feed

- Currently-recording files are served as a live HTTP stream:
  `GET /api/recordings/play?path=<relative-path>`. Open this URL in a player
  (e.g. VLC) rather than the raw SMB file, which would hang on a growing file.
- `GET /api/podcast?folder=` returns an RSS feed of recordings for phone
  playback. Set `RADIOTIMER_PUBLIC_URL` when the service sits behind a proxy.

## Error logs

ffmpeg's stderr is captured in memory; a `<recording>.error.txt` file is written
next to the recording **only** when ffmpeg exits with a failure code.

## API overview

| Method                | Path                          | Purpose              |
|-----------------------|-------------------------------|----------------------|
| GET/POST/PUT/DELETE   | `/api/stations` (+ `/{id}`)   | Station CRUD         |
| GET                   | `/api/stations/{id}/open`     | Open station stream  |
| GET/POST/PUT/DELETE   | `/api/schedules` (+ `/{id}`)  | Schedule CRUD        |
| GET                   | `/api/status`                 | Running recordings   |
| GET                   | `/api/recordings`             | Recordings tree      |
| GET                   | `/api/recordings/play`        | Live / timeshift stream |
| GET                   | `/api/events`                 | SSE (state / ping)   |
| GET                   | `/api/podcast`                | RSS feed             |

## Tests

```bash
cd recording-service && pytest        # pytest.ini sets asyncio_mode=auto
```
