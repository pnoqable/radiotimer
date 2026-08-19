# Entwicklungsplan: radiotimer

Selbstgehosteter, schlanker Webdienst zum automatischen Aufnehmen von
Webradiosendungen nach Zeitplan — Nachfolger des alten "vlc timer"
(Qt/Windows). Basis: Fork von `holstt/stream2podcast` (Python, APScheduler,
Docker). Upstream-History bleibt erhalten (Remote `upstream`).

## Aktueller Stand (implementiert)

- [x] **Phase 1**: Repo als Fork (`upstream` → holstt/stream2podcast), Smoke-Test grün.
- [x] **Phase 2**: Recording-Kern auf **ffmpeg** umgestellt (`src/ffmpeg_recorder.py`,
      Reconnect + sauberes SIGTERM-Ende via `-t`).
- [x] **Phase 3**: **Eigene `stream_url` pro Sendung** (`models.py`, `schedule_builder.py`).
- [x] **Phase 4**: **SQLite**-Store (`src/db.py`) + **FastAPI-API** (CRUD, Status, Recordings).
- [x] **Phase 5**: **Schlanke Web-UI** (`static/index.html`): anlegen/bearbeiten/löschen,
      Live-Status, Aufnahmen-Liste mit Player/Download.
- [x] **Phase 6 (Teil)**: Docker-Single-Service mit ffmpeg (`Dockerfile`, `docker-compose.yml`);
      `feed-service` entfernt. Windows-Anleitung steht noch aus.
- [ ] **Phase 7**: Polish (Retention, Fehler-Alerts, Windows-README, Tests).

Getestet lokal (macOS, venv): Scheduler lädt Sendungen aus der DB und berechnet
`next_run` korrekt (mo–fr 20:00 MESZ → 18:00 UTC).

## Ausgangslage & Lücken im Basis-Projekt

stream2podcast kann bereits: Zeitpläne (APScheduler/cron), ICY- und HLS-Streams,
Docker-Deploy, Podcast-RSS-Feed. Es fehlt bzw. muss geändert werden:

1. **Kein ffmpeg** — es piped rohe Stream-Bytes per `aiohttp` auf Platte.
   Fragil (kein Reconnect, kein Muxing, keine Formatumwandlung).
   → Kern wird auf ffmpeg-Subprocess umgestellt.
2. **Nur eine globale `stream_url`** für *alle* Sendungen.
   → Jede Sendung braucht eine eigene Stream-URL.
3. **Keine Web-UI** — nur YAML-Config + RSS.
   → Schlanke FastAPI-Web-UI für Verwaltung + Status.
4. **Keine Persistenz** — reine YAML-Datei.
   → SQLite als Schedule-Store für Web-UI-CRUD.

## Empfohlene Zielarchitektur

- **Sprache/Framework**: Python + FastAPI (Web-UI + Management-API),
  APScheduler (bleibt) für den Zeitplan.
- **Recording**: `ffmpeg` als externer Binary-Prozess (nicht als Lib):
  `ffmpeg -reconnect 1 -reconnect_streamed 1 -i <url> -t <sek> -c copy <ziel>`.
  Sauberes Beenden via `SIGTERM` (ffmpeg schneidet sauber ab, kein `kill -9`).
- **Persistenz**: SQLite (`schedules`-Tabelle). YAML als optionaler Import/Export.
- **Frontend**: HTML + vanilla JS (oder htmx/Alpine) — bewusst KEINE schwere SPA.
- **Deploy**: Docker (Debian-Slim + ffmpeg im Image), Volume für Aufnahmen + DB.
  Läuft auf Windows via Docker Desktop / WSL2. Optionale native Windows-Variante
  (ffmpeg.exe + Python) wird dokumentiert.

## Phasen

### Phase 1 — Repo & Build grün bekommen
- [x] Fork/Clone als `radiotimer/`, Remote `upstream` gesetzt.
- [ ] `recording-service` lokal mit Poetry installierbar/buildbar (baseline).
- [ ] `docker/docker-compose.yml` verstehen, Image baut mit ffmpeg-Vorabcheck.

### Phase 2 — Recording-Kern auf ffmpeg umstellen
- [ ] `src/audio_stream.py` + `src/recording_service.py` ersetzen durch
      ffmpeg-Subprocess-Wrapper (Start, Timeout, SIGTERM, Exit-Code-Check).
- [ ] Robustheit: `-reconnect`, `-reconnect_streamed`, `-reconnect_delay_max`.
- [ ] Formatwahl (mp3/mp4) je Sendung beibehalten.

### Phase 3 — Pro-Sendung Stream-URL
- [ ] `RecordingSchedule` (models.py) + Config-Schema: eigenes `stream_url`
      pro Schedule, Fallback auf globalen Default.
- [ ] `record_audio_task` erhält die URL des jeweiligen Tasks (nicht global).

### Phase 4 — Persistenz (SQLite) + Management-API
- [ ] SQLite-Store für Schedules (anstelle/ergänzend zu YAML).
- [ ] FastAPI-Endpunkte:
      `GET/POST/PUT/DELETE /api/schedules`,
      `GET /api/recordings`, `GET /api/status` (laufende Aufnahmen),
      optional `POST /api/recordings/{id}/download`.
- [ ] Scheduler lädt Jobs aus der DB; Änderungen an Schedules -> Job (re)load.

### Phase 5 — Web-UI (schlank)
- [ ] Sendungen auflisten / anlegen / bearbeiten / löschen.
- [ ] Live-Status laufender Aufnahmen + Liste bisheriger Aufnahmen (Download).
- [ ] Optional: Inline-Audio-Player für Aufnahmen.

### Phase 6 — Docker & Deploy
- [ ] Image: `python:3.12-slim` + `ffmpeg` (apt), ffmpeg im PATH.
- [ ] compose: ein Service (recording + web) oder web als zweiter Container;
      Volumes für `recordings/` + `app.db`; Port exposed; Healthcheck.
- [ ] Windows-Anleitung: Docker Desktop (WSL2) + optional native Variante.

### Phase 7 — Optional / Polish
- [ ] Podcast-Feed-Service (feed-service) behalten? (Hören auf Handy) — sonst streichen.
- [ ] Logging, Fehler-Alerts (z.B. Aufnahme fehlgeschlagen), Restart-Strategie.
- [ ] Retention: alte Aufnahmen nach N Tagen löschen.

## Offene Entscheidungen (bitte bestätigen)
- Web-UI in den `recording-service` integrieren (einfach) oder eigener
  `web-service`-Container (sauberer)?
- Podcast-RSS-Feed gewünscht (Handy-Wiedergabe) oder reine Dateiablage?
- Reicht SQLite, oder lieber JSON-Datei als Schedule-Store (noch schlanker)?
