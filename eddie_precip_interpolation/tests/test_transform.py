import math

import pandas as pd

from eddie_precip_interpolation.transform import double_value


def test_double_value_doubles_populated_rows():
    df = pd.DataFrame({
        "station_id": ["AGM_013"],
        "datetime": ["2026-09-25T01:00:00Z"],
        "value": [3.5],
    })

    result = double_value(df)

    assert result.loc[0, "value_doubled"] == 7.0


def test_double_value_null_stays_null_not_zero():
    df = pd.DataFrame({
        "station_id": ["AGM_028"],
        "datetime": ["2026-09-29T09:00:00Z"],
        "value": [math.nan],
    })

    result = double_value(df)

    assert math.isnan(result.loc[0, "value_doubled"])
