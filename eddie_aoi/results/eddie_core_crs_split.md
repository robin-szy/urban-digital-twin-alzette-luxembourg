# EDDIE core: split `to_working_crs_rectangle` (candidate upstream contribution)

*Robin, 2026-11-26. Tasks 13.1-13.3 of `openspec/changes/aoi-reach-variants`.*

## Why

`eddie.digitaltwin.working_crs.to_working_crs_rectangle` did two things in one function: it reprojected
the area of interest to the working CRS **and** replaced it with its bounding rectangle. That is right
for sources that only accept bounds, and wrong for a clip mask or an analysis domain: a corridor along
a river becomes a rectangle several times its area. The AOI module needs both roles kept apart
(fetch envelope vs. clip mask / analysis domain).

## The change

- `to_working_crs(aoi)`: reprojects, keeping the shape.
- `fetch_envelope(aoi)`: the working-CRS bounding rectangle, for bounds-only sources. Never a clip
  mask or a denominator.
- `to_working_crs_rectangle(aoi)`: kept as a deprecated alias of `fetch_envelope`, so no
  existing caller breaks.
- Callers repointed by role. Both are bounds-only, so both use `fetch_envelope`:
  `eddie.tasks.wkt_to_gdf` (feeds `retrieve_from_instructions` via `add_base_data_to_db`) and
  `eddie.run_all.create_sample_polygon`.
- Tests: `fetch_envelope` returns a rectangle containing the input, `to_working_crs` returns a bent
  corridor unchanged (area < 60 % of its envelope), and the old name equals `fetch_envelope`.

Behaviour is unchanged for every existing caller.

## Verified

- Core test suite run in the worker against the new source: 41 passed, 3 failed. The 3 failures
  (`test_setup_environment` x2, `test_discover_plugins` x1) fail identically on the installed
  (old) code. The cause was the database's connection limit at the time ("too many clients"), not this
  change. All `test_working_crs` tests pass.
- Backend image rebuilt with the change; worker startup ran `add_base_data_to_db` successfully (13 s).

## Scope check

The diff touches only `working_crs.py`, its two callers and its test file. No other CRS code.

## Diff

```diff
diff --git a/EDDIE/eddie_antarctica/lib/eddie/src/eddie/digitaltwin/working_crs.py b/EDDIE/eddie_antarctica/lib/eddie/src/eddie/digitaltwin/working_crs.py
index 10bebd7..6dcff17 100644
--- a/EDDIE/eddie_antarctica/lib/eddie/src/eddie/digitaltwin/working_crs.py
+++ b/EDDIE/eddie_antarctica/lib/eddie/src/eddie/digitaltwin/working_crs.py
@@ -64,9 +64,32 @@ def resolve_working_crs(area_of_interest: Optional[gpd.GeoDataFrame] = None) ->
     return 4326
 
 
-def to_working_crs_rectangle(area_of_interest: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
+def to_working_crs(area_of_interest: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
+    """
+    Reproject the area of interest to the working CRS, keeping its shape.
+
+    For a clip mask or an analysis domain: a corridor stays a corridor. Use fetch_envelope for
+    sources that only accept bounds.
+
+    Parameters
+    ----------
+    area_of_interest : gpd.GeoDataFrame
+        The area of interest, in any CRS.
+
+    Returns
+    -------
+    gpd.GeoDataFrame
+        The same geometries in the working CRS.
+    """
+    return area_of_interest.to_crs(resolve_working_crs(area_of_interest))
+
+
+def fetch_envelope(area_of_interest: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
     """
-    Reproject the area of interest to the working CRS and replace it with its bounding rectangle.
+    The bounding rectangle of the area of interest in the working CRS, for bounds-only sources.
+
+    Never use it as a clip mask or as the denominator of a statistic: it covers more than the
+    area of interest (use to_working_crs for those).
 
     Parameters
     ----------
@@ -78,5 +101,15 @@ def to_working_crs_rectangle(area_of_interest: gpd.GeoDataFrame) -> gpd.GeoDataF
     gpd.GeoDataFrame
         A single rectangular polygon in the working CRS.
     """
-    reprojected = area_of_interest.to_crs(resolve_working_crs(area_of_interest))
+    reprojected = to_working_crs(area_of_interest)
     return gpd.GeoDataFrame(index=[0], crs=reprojected.crs, geometry=[shapely.box(*reprojected.total_bounds)])
+
+
+def to_working_crs_rectangle(area_of_interest: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
+    """
+    Deprecated alias of fetch_envelope, kept so existing callers keep working.
+
+    The name hid two roles in one function: reprojecting (to_working_crs) and replacing the
+    shape by its bounding rectangle (fetch_envelope). Call the one that matches the role.
+    """
+    return fetch_envelope(area_of_interest)
diff --git a/EDDIE/eddie_antarctica/lib/eddie/src/eddie/run_all.py b/EDDIE/eddie_antarctica/lib/eddie/src/eddie/run_all.py
index d6dcca6..5a00283 100644
--- a/EDDIE/eddie_antarctica/lib/eddie/src/eddie/run_all.py
+++ b/EDDIE/eddie_antarctica/lib/eddie/src/eddie/run_all.py
@@ -24,7 +24,7 @@ from typing import Dict, Union
 import geopandas as gpd
 
 from eddie.digitaltwin import cache_new_results
-from eddie.digitaltwin.working_crs import to_working_crs_rectangle
+from eddie.digitaltwin.working_crs import fetch_envelope
 
 
 def main(
@@ -75,5 +75,5 @@ def create_sample_polygon() -> gpd.GeoDataFrame:
     # Convert to WGS84 to deliberately introduce rounding errors. Ensures our development acts like production.
     # These rounding errors occur in production when serialising WGS84 polygons
     aoi = aoi.to_crs(4326)
-    # Convert the polygon to the working crs, and recalculate the bounds to ensure it is a rectangle.
-    return to_working_crs_rectangle(aoi)
+    # The base data sources accept bounds only: the working-CRS bounding rectangle of the polygon.
+    return fetch_envelope(aoi)
diff --git a/EDDIE/eddie_antarctica/lib/eddie/src/eddie/tasks.py b/EDDIE/eddie_antarctica/lib/eddie/src/eddie/tasks.py
index 6f516d7..e929abc 100644
--- a/EDDIE/eddie_antarctica/lib/eddie/src/eddie/tasks.py
+++ b/EDDIE/eddie_antarctica/lib/eddie/src/eddie/tasks.py
@@ -33,7 +33,7 @@ from eddie.config import EnvVariable
 from eddie.digitaltwin import retrieve_from_instructions
 from eddie.digitaltwin.utils import retry_function, setup_logging
 from eddie.discover_plugins import discover_plugins
-from eddie.digitaltwin.working_crs import to_working_crs_rectangle
+from eddie.digitaltwin.working_crs import fetch_envelope
 
 # Setup celery backend task management
 message_broker_url = f"redis://{EnvVariable.MESSAGE_BROKER_HOST}:6379/0"
@@ -109,8 +109,8 @@ def wkt_to_gdf(wkt: str) -> gpd.GeoDataFrame:
     """
     selected_polygon = gpd.GeoDataFrame(index=[0], crs="epsg:4326", geometry=[shapely.from_wkt(wkt)])
 
-    # Convert the polygon to the working crs, and recalculate the bounds to ensure it is a rectangle.
-    return to_working_crs_rectangle(selected_polygon)
+    # The base data sources accept bounds only: the working-CRS bounding rectangle of the polygon.
+    return fetch_envelope(selected_polygon)
 
 
 # Plugins must be imported after app to remove a circular dependency
diff --git a/EDDIE/eddie_antarctica/lib/eddie/tests/test_digitaltwin/test_working_crs.py b/EDDIE/eddie_antarctica/lib/eddie/tests/test_digitaltwin/test_working_crs.py
index 9513b6f..69fb9c1 100644
--- a/EDDIE/eddie_antarctica/lib/eddie/tests/test_digitaltwin/test_working_crs.py
+++ b/EDDIE/eddie_antarctica/lib/eddie/tests/test_digitaltwin/test_working_crs.py
@@ -6,7 +6,8 @@ import geopandas as gpd
 import shapely
 
 from eddie.config import EnvVariable
-from eddie.digitaltwin.working_crs import resolve_working_crs, to_working_crs_rectangle
+from eddie.digitaltwin.working_crs import (fetch_envelope, resolve_working_crs, to_working_crs,
+                                           to_working_crs_rectangle)
 
 
 def aoi(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> gpd.GeoDataFrame:
@@ -58,6 +59,32 @@ class ResolveWorkingCrsTest(unittest.TestCase):
         self.assertAlmostEqual(geometry.area, geometry.envelope.area)
         self.assertTrue(geometry.contains(triangle.to_crs(32759).geometry[0]))
 
+    def test_fetch_envelope_returns_a_rectangle(self):
+        corridor = gpd.GeoDataFrame(index=[0], crs=4326, geometry=[
+            shapely.LineString([(172.5, -43.6), (172.6, -43.5), (172.7, -43.6)]).buffer(0.01)])
+        envelope = fetch_envelope(corridor)
+        self.assertEqual(envelope.crs.to_epsg(), 32759)
+        self.assertEqual(len(envelope), 1)
+        geometry = envelope.geometry[0]
+        self.assertAlmostEqual(geometry.area, geometry.envelope.area)
+        self.assertTrue(geometry.contains(corridor.to_crs(32759).geometry[0]))
+
+    def test_to_working_crs_keeps_the_shape(self):
+        corridor = gpd.GeoDataFrame(index=[0], crs=4326, geometry=[
+            shapely.LineString([(172.5, -43.6), (172.6, -43.5), (172.7, -43.6)]).buffer(0.01)])
+        reprojected = to_working_crs(corridor)
+        self.assertEqual(reprojected.crs.to_epsg(), 32759)
+        geometry = reprojected.geometry[0]
+        expected = corridor.to_crs(32759).geometry[0]
+        # A bent corridor is far smaller than its rectangle, and is returned unchanged.
+        self.assertLess(geometry.area, 0.6 * geometry.envelope.area)
+        self.assertAlmostEqual(geometry.symmetric_difference(expected).area, 0.0, places=3)
+
+    def test_old_name_is_the_envelope(self):
+        triangle = gpd.GeoDataFrame(index=[0], crs=4326,
+                                    geometry=[shapely.Polygon([(172.5, -43.6), (172.7, -43.6), (172.6, -43.4)])])
+        self.assertTrue(to_working_crs_rectangle(triangle).geometry[0].equals(fetch_envelope(triangle).geometry[0]))
+
 
 if __name__ == "__main__":
     unittest.main()
\ No newline at end of file
```
