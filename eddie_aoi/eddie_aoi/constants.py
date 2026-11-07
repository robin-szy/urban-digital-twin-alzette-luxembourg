"""Project-wide constants for the AOI module.

The CRS rule: compute and store in ``METRIC_CRS``; reproject to
``PUBLISH_CRS`` only when handing geometry to a map client, and never read
geographic coordinates back as the source for a measurement. At Luxembourg's
latitude one degree of longitude is roughly 72.3 km against 111.2 km for one
degree of latitude, so an area in square degrees is meaningless and a buffer
in degrees is anisotropic - both silently.
"""

#: Everything is computed and stored in this CRS. Metres.
METRIC_CRS = "EPSG:2169"

#: Only for output to a map client. Degrees.
PUBLISH_CRS = "EPSG:4326"

#: Reach identifiers are canonical at this precision. The marker source
#: spaces points at 100 m and its kilometre values carry float noise, so
#: anything finer would fragment the cache on rounding alone.
REACH_ID_PRECISION_M = 1

#: Kilometre identifiers are zero-padded to this width, in metres.
REACH_ID_DIGITS = 6

#: Tolerance when selecting markers by kilometre value. The source's KILO_KM
#: carries float noise (46.299999 for 46.3); this is far below the 100 m
#: point spacing.
MARKER_SELECTION_KM_TOLERANCE = 1e-3
