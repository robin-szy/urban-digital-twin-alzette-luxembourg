# gauge_collector

A small service that fetches the water-level readings of the Alzette and its
gauged tributaries (Attert, Eisch, Mamer, Wark) from the Héichwaasser API of
the Administration de la gestion de l'eau (https://heichwaasser.lu/api/v1, the
data behind inondations.lu) and adds them to `water_level_history` in the
project's PostGIS. Readings already stored are skipped.

The 3D water surface shows the latest stored Pfaffenthal and Hesperange
readings by default (backend `GET /water-level/latest`).

It is deliberately not an `eddie_*` package, so EDDIE's plugin discovery leaves it alone.
It runs in its own container (`gauge_collector` in `EDDIE/eddie_antarctica/docker-compose.override.yml`),
built from the backend image.

## Source

- One reading every 15 minutes per station, published with about 30-60 minutes delay.
- The full history is kept, back to 2017-12-14, so a gap in collection can always be filled later.
- Timestamps are stored like `eddie_water_level.tasks.load_water_level_period` stores them
  (the API's local-time stamps labelled UTC), so both writers produce the same rows.

## Running it

From `EDDIE/eddie_antarctica`:

```bash
# Start the service (and the database); it collects every 15 minutes while it runs.
docker compose up -d gauge_collector

# Once: load the whole history (about 20 stations x 9 years, several minutes).
docker compose exec gauge_collector /venv/bin/python -m gauge_collector backfill
#   or through the API, in the background:  curl -X POST http://localhost:5002/backfill

# Incremental run now (the same as the 15-minute schedule).
docker compose exec gauge_collector /venv/bin/python -m gauge_collector collect
#   or:  curl -X POST http://localhost:5002/collect

# What is stored.
curl http://localhost:5002/status
```

Without the service running, a one-off run also works:

```bash
docker compose run --rm gauge_collector collect
```

## API

| Method | Path        | What it does |
|--------|-------------|--------------|
| GET    | `/`         | Service info and the last runs |
| GET    | `/status`   | Readings stored per station (count, first, last) |
| POST   | `/collect`  | Incremental run: from each station's last stored reading to now |
| POST   | `/backfill` | Background history load, `?start=YYYY-MM-DD&end=YYYY-MM-DD` (default: all) |

## Settings

| Variable | Default | |
|----------|---------|---|
| `COLLECT_INTERVAL_MIN` | `15` | Minutes between incremental runs while the service is up; `0` switches the schedule off |
| `GAUGE_RIVER_IDS` | `17,23,13,15,9` | Héichwaasser river ids: Alzette, Attert, Eisch, Mamer, Wark |
| `GAUGE_API_ROOT` | `https://heichwaasser.lu/api/v1` | |
| `POSTGRES_*` | from `.env` / `.env.docker-override` | The project's database |
