import pandas as pd

# The source's precipitation variable code, per data_sources/asta_precipitation.yml
# (fetch.variable). Hardcoded here rather than read from that yml - accepted
# coupling, see design.md "Risks / Trade-offs".
PRECIPITATION_VARIABLE = "sum_nn050"


def double_value(observations_df: pd.DataFrame) -> pd.DataFrame:
    """Placeholder transform standing in for real spatial interpolation - just
    proves the derive-and-publish path works end to end. A null value stays
    null (NaN * 2 is still NaN), never becomes 0.
    """
    result = observations_df.copy()
    result["value_doubled"] = result["value"] * 2
    return result
