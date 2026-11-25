# Snippet 1 — mount the new plugin (required)

`EDDIE/eddie_antarctica/docker-compose.override.yml`

EDDIE finds plugins by scanning `site-packages` for packages named `eddie_*`, so the new
package has to be mounted there, exactly like the existing ones. Add **one line to each of
the two services** (`backend` and `celery_worker`, next to the `eddie_building_population`
mount that is already there):

```yaml
      - ../../eddie_water_fees/eddie_water_fees:/venv/lib/python3.11/site-packages/eddie_water_fees:ro
```

Result, in both services:

```yaml
      - ../../eddie_building_population/eddie_building_population:/venv/lib/python3.11/site-packages/eddie_building_population:ro
      - ../../eddie_water_fees/eddie_water_fees:/venv/lib/python3.11/site-packages/eddie_water_fees:ro
```

A **new** mount is not picked up by `docker restart`; recreate just those two services
(no image build):

```powershell
cd EDDIE/eddie_antarctica
docker compose up -d --no-build --no-deps backend celery_worker
```

After this first time, editing the plugin only needs:

```powershell
docker restart backend_digital_twin celery_worker_digital_twin
```

Check that both containers see it and that the routes exist:

```powershell
docker exec backend_digital_twin ls /venv/lib/python3.11/site-packages/eddie_water_fees
curl.exe -s http://localhost:5000/water-fees/summary.json
```

`{"status":"no_data", ...}` is the correct answer before the first run. A 404 means the
mount or the restart did not happen, and the backend log says which import failed:

```powershell
docker logs backend_digital_twin --tail 50
```

If the package is mounted but EDDIE crashes at start, it is almost always a missing
`tasks.py` or `blueprint.py` — the plugin contract needs both (they are both included).
