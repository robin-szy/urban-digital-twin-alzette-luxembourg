import math

import sqlalchemy as sa

from eddie_precip_interpolation.db import read_precipitation_observations

OBSERVATIONS_TABLE = "weather_observations_test"


def _make_observations_table(conn) -> sa.Table:
    table = sa.Table(
        OBSERVATIONS_TABLE,
        sa.MetaData(),
        sa.Column("station_id", sa.Text),
        sa.Column("datetime", sa.Text),
        sa.Column("variable", sa.Text),
        sa.Column("value", sa.Float),
    )
    table.create(conn)
    return table


def test_read_precipitation_observations_filters_variable_and_keeps_null(sqlite_conn):
    table = _make_observations_table(sqlite_conn)
    sqlite_conn.execute(table.insert(), [
        {"station_id": "AGM_013", "datetime": "2026-09-25T01:00:00Z", "variable": "sum_nn050", "value": 1.5},
        {"station_id": "AGM_028", "datetime": "2026-09-29T09:00:00Z", "variable": "sum_nn050", "value": None},
        # A different variable for the same station/time must be excluded.
        {"station_id": "AGM_013", "datetime": "2026-09-25T01:00:00Z", "variable": "avg_ta200", "value": 12.0},
    ])
    sqlite_conn.commit()

    result = read_precipitation_observations(sqlite_conn, OBSERVATIONS_TABLE)

    assert len(result) == 2
    assert set(result["station_id"]) == {"AGM_013", "AGM_028"}
    null_row = result[result["station_id"] == "AGM_028"].iloc[0]
    assert null_row["value"] is None or math.isnan(null_row["value"])
