# eddie_precip_interpolation

An EDDIE plugin that derives a layer from the precipitation observations
`eddie_alzette_ingest` already wrote to Postgres. Today it applies a
placeholder transform - `value * 2` - instead of real spatial
interpolation. The point of this plugin, as it stands, is to prove the
"read from DB -> transform -> publish to GeoServer -> auto-appears in the
Terria catalog" path works end to end, before the real interpolation
algorithm replaces the transform.

## Layout

- `eddie_precip_interpolation/transform.py` - the placeholder transform
  (`double_value`), plain pandas, no `eddie.*` import.
- `eddie_precip_interpolation/db.py` - reading observations (SQL, no
  geometry), reading station geometry (needs real PostGIS), merging the two
  (pure, no DB), and the upsert write of the derived table. No `eddie.*`
  import either.
- `eddie_precip_interpolation/tasks.py` - the only file that imports
  `eddie.*`; defines the Celery task
  `eddie_precip_interpolation.tasks.derive_precipitation`.
- `eddie_precip_interpolation/blueprint.py` - an empty Flask blueprint.
  EDDIE's backend imports `<plugin>.blueprint` for every discovered
  `eddie_*` package on startup; without this file the backend container
  crashes.

Read/write functions are deliberately split by whether they touch a real
PostGIS geometry column: the geometry-free parts (`read_precipitation_observations`,
`double_value`, `attach_station_geometry`) are unit-tested, including on
SQLite; the geometry-bearing parts (`read_station_geometries`,
`write_derived_table`) need real PostGIS and are `@pytest.mark.integration`
only - the same boundary `eddie_alzette_ingest/db.py` already draws around
its own stations table.

## Running the tests (no EDDIE, no Docker, no network needed)

```bash
pip install -e eddie_precip_interpolation[dev]
pytest eddie_precip_interpolation/tests
```

The `@pytest.mark.integration` test additionally needs a running PostGIS
(the `db_postgres_digital_twin` container) and is skipped by default:

```bash
TEST_POSTGRES_PASSWORD=<password> pytest eddie_precip_interpolation/tests -m integration
```

## Running the derive step outside EDDIE (plain Python, for local testing)

```python
import sqlalchemy as sa
from eddie_precip_interpolation import db
from eddie_precip_interpolation.transform import double_value

engine = sa.create_engine("postgresql+psycopg2://postgres:<password>@localhost:5431/db")
with engine.connect() as conn:
    observations_df = db.read_precipitation_observations(conn, "weather_observations")
    doubled_df = double_value(observations_df)
    stations_gdf = db.read_station_geometries(conn, "weather_stations")
    derived_gdf = db.attach_station_geometry(doubled_df, stations_gdf)
    db.ensure_derived_table(conn, "precipitation_doubled")
    db.write_derived_table(conn, derived_gdf, "precipitation_doubled")
```

## Running as an EDDIE plugin

Needs the EDDIE stack running (`EDDIE/eddie_antarctica`) with this package
installed into the same Python environment as `eddie`/`eddie_antartica`
(pip-installed, discovered by `pkgutil.iter_modules()` - see the EDDIE
core's `discover_plugins`). Like `eddie_alzette_ingest`, it currently
reaches the running containers only via a `docker-compose.override.yml`
bind-mount into `site-packages`, not a real `pip install` baked into the
image - see the main repo's HW4 notes on that gap.

Once installed, trigger manually (no scheduler, no AOI - this plugin
processes every stored `sum_nn050` observation each run):

```bash
docker compose exec celery_worker bash -lc \
  "source /venv/bin/activate && celery -A src.eddie_antartica.tasks call \
   eddie_precip_interpolation.tasks.derive_precipitation"
```

## What it stores

One table, `precipitation_doubled` (`station_id`, `datetime`,
`value_doubled`, `geometry`), one row per source `sum_nn050` observation
that has a matching station. Upserted keyed by `(station_id, datetime)`:
unlike `eddie_alzette_ingest`'s append-only observations table, re-running
this task overwrites `value_doubled` - a derived value is recomputed from
the same transform each run, not an immutable fact. Published as a
GeoServer vector layer in the `input_layers` workspace, which is why it
appears in `GET /terria-catalog.json` with no frontend code changes.

## Real interpolation module - first implementation milestone

`eddie_precip_interpolation/interpolate.py` now contains the pure, EDDIE-independent
precipitation engine.  The existing `tasks.py` still calls the placeholder
`double_value()` on purpose: integration is changed only after the scientific
function and its tests are stable.

Current scientific defaults (all configurable):

- metric CRS: EPSG:2169
- AOI/grid resolution: 100 m
- IDW power: p=1 (provisional; first LOOCV result)
- dynamic station selection: up to 5 nearest valid gauges within 15 km
- minimum 3 valid gauges
- missing observations are excluded, never replaced with zero
- optional fallback radius is explicit and disabled by default
- every grid cell returns P(x,y,t) plus quality metadata Q(x,y,t): station
  count, nearest/mean station distance, convex-hull support,
  interpolation/extrapolation status, fallback status and quality class

`power`, `max_stations`, and `search_radius_m` are deliberately parameters so
that a later historical cross-validation can jointly tune p, N and radius
without rewriting the EDDIE integration.
