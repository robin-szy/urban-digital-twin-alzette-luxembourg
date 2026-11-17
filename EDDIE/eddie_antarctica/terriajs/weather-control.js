// Weather layer of the Alzette digital twin (TerriaJS / Cesium).
//
// What it does
//   One panel with four time modes, draped weather fields on the 3D terrain, 3D wind streamlines, a rain
//   effect at ground level, eye-level viewpoints at the gauges, summary cards and a chart of rain
//   and water level.
//     History   one chosen day since 1950, replayed hour by hour (Open-Meteo archive: ERA5 / ERA5-Land)
//     Now       the last days and the next hours (Open-Meteo forecast model; ASTA stations when reachable)
//     Forecast  up to 16 days, with an uncertainty band from an ensemble forecast
//     2050      climate statistics from CMIP6 models, never shown as daily weather on the map
//
// Design decisions (state them in the presentation)
//   - The visual part never fetches data itself: it asks PROVIDERS (below). Today the providers call
//     Open-Meteo from the browser and the existing EDDIE endpoints. When the eddie_weather ingestion
//     exists, only the providers change; the panel, the map and the chart stay as they are.
//   - The area follows Robin's AOI selector: the reach currently on Terria's workbench, or the
//     configured fallback reach. Its "watersheds" variant is the averaging area when it exists;
//     a substitute is named as such in the panel, never hidden.
//   - The weather field is interpolated (inverse distance, power 2) between model grid points about
//     2.5 km apart. Model grids are several km wide, so the field is smooth on purpose: it must not
//     look more detailed than the data.
//   - Precipitation of an hour is the sum of the PRECEDING hour (Open-Meteo convention).
//   - Gauge times from EDDIE are local wall-clock times labelled UTC (documented in gauge_collector).
//     CONFIG.waterTimesAreLocal converts them; set it to false once the collector stores real UTC.
//   - Climate projections (to 2049) are compared as 30-year statistics, 1981-2010 against 2021-2049.
//
// $BACKEND_URL is filled in by entrypoint.sh, like the other Terria scripts.
(function (root) {
  "use strict";

  // =============================================================================================
  // CONFIGURATION
  // =============================================================================================

  var CONFIG = {
    backendUrl: "$BACKEND_URL",
    fallbackReachId: "alzette_035000_050000",
    // Averaging area, in order of preference, matched on Robin's variant names.
    areaVariantPreference: ["watersheds", "buffer", "bbox", "corridor"],
    // The weather is drawn over the area's bounding box widened by this margin.
    areaMarginKm: 3,
    latticeSpacingKm: 2.5,
    latticeMaxPoints: 36,
    // Interpolation grid of the drawn field (cells per side).
    fieldSize: 64,
    idwPower: 2,
    openMeteo: {
      forecast: "https://api.open-meteo.com/v1/forecast",
      archive: "https://archive-api.open-meteo.com/v1/archive",
      ensemble: "https://ensemble-api.open-meteo.com/v1/ensemble",
      climate: "https://climate-api.open-meteo.com/v1/climate"
    },
    // Tried in order until one answers: Open-Meteo has renamed ensemble models before.
    ensembleModels: ["ecmwf_ifs025", "ecmwf_ifs025_ensemble", "gfs025", "icon_seamless"],
    climateModels: ["EC_Earth3P_HR", "MPI_ESM1_2_XR", "CMCC_CM2_VHR4", "MRI_AGCM3_2_S"],
    heavyRainMm: 20,
    baseline: [1981, 2010],
    future: [2021, 2049],
    historyFirstDate: "1950-01-01",
    historyLagDays: 2,
    // History takes a period of days. Hourly data for the whole period is fetched at once, so the
    // period has a longest length; the gauge line is fetched day by day, so it has a shorter one.
    historyMaxDays: 92,
    historyWaterMaxDays: 31,
    forecastDays: 16,
    nowPastDays: 7,
    nowWindowPastH: 48,
    nowWindowAheadH: 24,
    nowRefreshMin: 15,
    // How long an answer already fetched is shown again without asking the source (minutes).
    keepNowMin: 10,
    keepForecastMin: 30,
    keepPastMin: 720,
    // Boxes of other modules that the weather panels must not cover: Terria's feature information
    // box (Devika's flood damage popup opens in it) and Devika's "Popup comparison" box.
    yieldTo: ["[class*='feature-info-panel__panel']", "#eddie-flood-cost-comparison"],
    astaLiveUrl: "https://wms.inspire.geoportail.lu/geoserver/ml/wfs?service=WFS&version=2.0.0" +
      "&request=GetFeature&typeNames=MF.PointTimeSeriesObservation_Live_ASTA_sum_nn050" +
      "&outputFormat=application/json&count=500",
    waterTimesAreLocal: true,
    // River kilometres covered by Luiz's 3D water surface (RIVER_BEGIN_KM, RIVER_END_KM).
    waterReachKm: [35, 50],
    waterHistoryStart: "2017-12-14",
    // Luiz's water item on Terria's layer list, and how long a river view may take to arrive
    // before the next one is asked for anyway (seconds).
    waterItemPrefix: "alzette-3d-water-surface",
    // In Now and Forecast the river is drawn at the latest gauge reading when it is at most this
    // old (hours); otherwise at its normal level.
    liveMaxAgeH: 3,
    // The day asked for is put back on the map when an older load replaces it: how often it is
    // checked (seconds) and how many times it is put back.
    riverKeepEveryS: 4,
    riverKeepTries: 4,
    // The map replays the whole History period up to this many days (the river data of a longer
    // period is too heavy to build at once: then it replays the day under the yellow band).
    // Luiz's replay runs one day in 144 seconds (600 times real time); a period runs faster in
    // proportion, so it also takes about that long.
    historyReplayMaxDays: 7,
    riverReplayMultiplier: 600,
    timeZone: "Europe/Luxembourg",
    hourlyVariables: [
      "precipitation", "temperature_2m", "relative_humidity_2m", "shortwave_radiation",
      "pressure_msl", "wind_speed_10m", "wind_direction_10m"
    ],
    // Wind streamlines: heights above the ground (m) asked from the source besides 10 m. The
    // forecast models give 120 m and 180 m, the archive gives 100 m only. Lines per height, from
    // the lowest up.
    windHeightsForecast: [120, 180],
    windHeightsArchive: [100],
    windLinesPerHeight: [7, 6, 5],
    cacheVersion: "wx1"
  };

  // Map layers. Precipitation uses classes (like the mockups), the others continuous ramps.
  // Ranges are fixed, so colours mean the same thing on every day and in every mode.
  var LAYERS = {
    precipitation: {
      label: "Precipitation", unit: "mm/h", variable: "precipitation", decimals: 1,
      // [from mm/h, colour, opacity]: light rain is mostly transparent, so a drizzle over the whole
      // area does not wash the map out in pale blue; heavier rain is more opaque.
      classes: [[0.1, "#8fcff3", 0.25], [0.5, "#5fb8ee", 0.4], [1, "#3a9fe8", 0.55], [2, "#1f80dc", 0.7],
        [5, "#1f52c4", 0.82], [10, "#3a2bb8", 0.9]]
    },
    temperature: {
      label: "Air temperature", unit: "°C", variable: "temperature_2m", decimals: 1,
      ramp: [[-10, "#2c3e91"], [0, "#3b82c4"], [10, "#7cc6a4"], [20, "#f0cf5a"], [30, "#e6722b"], [38, "#b11f2a"]]
    },
    humidity: {
      label: "Relative humidity", unit: "%", variable: "relative_humidity_2m", decimals: 0,
      ramp: [[30, "#e9d8a6"], [60, "#94d2bd"], [80, "#0a9396"], [100, "#005f73"]]
    },
    radiation: {
      label: "Solar radiation", unit: "W/m²", variable: "shortwave_radiation", decimals: 0,
      ramp: [[0, "#262a3b"], [100, "#5b4b8a"], [300, "#c0607a"], [600, "#f39c4a"], [900, "#ffe17a"]]
    },
    pressure: {
      label: "Sea-level pressure", unit: "hPa", variable: "pressure_msl", decimals: 0,
      ramp: [[990, "#6a3d9a"], [1005, "#4f8fd8"], [1015, "#9be7c4"], [1025, "#f4e285"], [1035, "#f08a4b"]]
    }
  };

  var MODES = {
    history: { label: "History" },
    now: { label: "Now" },
    forecast: { label: "Forecast" },
    climate: { label: "2050" }
  };

  // =============================================================================================
  // CORE: pure functions (no DOM, no Terria). Tested with node: tests/weather-core.test.js
  // =============================================================================================

  var DEG = Math.PI / 180;
  var EARTH_R = 6371000;

  function kmPerDegLon(lat) {
    return 111.32 * Math.cos(lat * DEG);
  }

  function expandBbox(bbox, km) {
    var midLat = (bbox.south + bbox.north) / 2;
    var dLat = km / 111.32;
    var dLon = km / kmPerDegLon(midLat);
    return { west: bbox.west - dLon, south: bbox.south - dLat, east: bbox.east + dLon, north: bbox.north + dLat };
  }

  // Regular lattice of sample points over a bbox, about spacingKm apart, at most maxPoints.
  function lattice(bbox, spacingKm, maxPoints) {
    var midLat = (bbox.south + bbox.north) / 2;
    var widthKm = (bbox.east - bbox.west) * kmPerDegLon(midLat);
    var heightKm = (bbox.north - bbox.south) * 111.32;
    var spacing = spacingKm;
    var nx, ny;
    for (;;) {
      nx = Math.max(2, Math.round(widthKm / spacing) + 1);
      ny = Math.max(2, Math.round(heightKm / spacing) + 1);
      if (nx * ny <= maxPoints) {
        break;
      }
      spacing *= 1.15;
    }
    var points = [];
    for (var j = 0; j < ny; j++) {
      for (var i = 0; i < nx; i++) {
        points.push({
          lon: bbox.west + (bbox.east - bbox.west) * i / (nx - 1),
          lat: bbox.south + (bbox.north - bbox.south) * j / (ny - 1)
        });
      }
    }
    return { points: points, nx: nx, ny: ny, spacingKm: spacing };
  }

  // Outer rings of a GeoJSON Polygon / MultiPolygon, as arrays of [lon, lat].
  function outerRings(geometry) {
    if (!geometry) {
      return [];
    }
    if (geometry.type === "Polygon") {
      return [geometry.coordinates[0]];
    }
    if (geometry.type === "MultiPolygon") {
      return geometry.coordinates.map(function (polygon) { return polygon[0]; });
    }
    if (geometry.type === "LineString") {
      return [];
    }
    return [];
  }

  function bboxOfCoordinates(coordinates, bbox) {
    var result = bbox || { west: Infinity, south: Infinity, east: -Infinity, north: -Infinity };
    (function walk(c) {
      if (typeof c[0] === "number") {
        result.west = Math.min(result.west, c[0]);
        result.east = Math.max(result.east, c[0]);
        result.south = Math.min(result.south, c[1]);
        result.north = Math.max(result.north, c[1]);
      } else {
        c.forEach(walk);
      }
    })(coordinates);
    return result;
  }

  function pointInRing(lon, lat, ring) {
    var inside = false;
    for (var i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      var xi = ring[i][0], yi = ring[i][1], xj = ring[j][0], yj = ring[j][1];
      if ((yi > lat) !== (yj > lat) && lon < (xj - xi) * (lat - yi) / (yj - yi) + xi) {
        inside = !inside;
      }
    }
    return inside;
  }

  function pointInRings(lon, lat, rings) {
    for (var r = 0; r < rings.length; r++) {
      if (pointInRing(lon, lat, rings[r])) {
        return true;
      }
    }
    return false;
  }

  // Distance in metres (equirectangular, fine at a few km).
  function distanceM(lon1, lat1, lon2, lat2) {
    var x = (lon2 - lon1) * DEG * Math.cos((lat1 + lat2) / 2 * DEG);
    var y = (lat2 - lat1) * DEG;
    return Math.sqrt(x * x + y * y) * EARTH_R;
  }

  // Inverse-distance weights from every field cell to every sample point: a (n*n) x P matrix,
  // computed once per area. A field is then W . values, and the area average is w . values.
  function idwMatrix(points, bbox, n, power) {
    var P = points.length;
    var W = new Float32Array(n * n * P);
    for (var j = 0; j < n; j++) {
      var lat = bbox.south + (bbox.north - bbox.south) * (j + 0.5) / n;
      for (var i = 0; i < n; i++) {
        var lon = bbox.west + (bbox.east - bbox.west) * (i + 0.5) / n;
        var base = (j * n + i) * P;
        var total = 0;
        for (var p = 0; p < P; p++) {
          var d = Math.max(distanceM(lon, lat, points[p].lon, points[p].lat), 1);
          var w = 1 / Math.pow(d, power);
          W[base + p] = w;
          total += w;
        }
        for (var q = 0; q < P; q++) {
          W[base + q] /= total;
        }
      }
    }
    return W;
  }

  // Cells of the field whose centre lies in the area.
  function areaMask(rings, bbox, n) {
    var mask = new Uint8Array(n * n);
    var count = 0;
    for (var j = 0; j < n; j++) {
      var lat = bbox.south + (bbox.north - bbox.south) * (j + 0.5) / n;
      for (var i = 0; i < n; i++) {
        var lon = bbox.west + (bbox.east - bbox.west) * (i + 0.5) / n;
        if (rings.length && pointInRings(lon, lat, rings)) {
          mask[j * n + i] = 1;
          count++;
        }
      }
    }
    return { mask: mask, count: count };
  }

  // Weights of the sample points in the area average: the mean, over the area's cells, of each
  // cell's interpolation weights. Sums to 1. Without area cells, every point weighs the same.
  function areaWeights(W, mask, n, P) {
    var weights = new Float64Array(P);
    var cells = 0;
    for (var c = 0; c < n * n; c++) {
      if (!mask[c]) {
        continue;
      }
      cells++;
      for (var p = 0; p < P; p++) {
        weights[p] += W[c * P + p];
      }
    }
    for (var q = 0; q < P; q++) {
      weights[q] = cells ? weights[q] / cells : 1 / P;
    }
    return weights;
  }

  // Field from sample values. Missing values are left out by renormalising the weights.
  function fieldFrom(W, values, n) {
    var P = values.length;
    var field = new Float32Array(n * n);
    var complete = values.every(function (v) { return v !== null && v !== undefined && isFinite(v); });
    for (var c = 0; c < n * n; c++) {
      var sum = 0, total = 0;
      for (var p = 0; p < P; p++) {
        var v = values[p];
        if (complete || (v !== null && v !== undefined && isFinite(v))) {
          sum += W[c * P + p] * v;
          total += W[c * P + p];
        }
      }
      field[c] = total > 0 ? sum / total : NaN;
    }
    return field;
  }

  function weightedMean(weights, values) {
    var sum = 0, total = 0;
    for (var p = 0; p < values.length; p++) {
      var v = values[p];
      if (v !== null && v !== undefined && isFinite(v)) {
        sum += weights[p] * v;
        total += weights[p];
      }
    }
    return total > 0 ? sum / total : null;
  }

  // Bilinear sample of a field at lon / lat (NaN outside).
  function sampleField(field, n, bbox, lon, lat) {
    var gx = (lon - bbox.west) / (bbox.east - bbox.west) * n - 0.5;
    var gy = (lat - bbox.south) / (bbox.north - bbox.south) * n - 0.5;
    if (gx < -0.5 || gy < -0.5 || gx > n - 0.5 || gy > n - 0.5) {
      return NaN;
    }
    gx = Math.min(Math.max(gx, 0), n - 1);
    gy = Math.min(Math.max(gy, 0), n - 1);
    var i0 = Math.floor(gx), j0 = Math.floor(gy);
    var i1 = Math.min(i0 + 1, n - 1), j1 = Math.min(j0 + 1, n - 1);
    var fx = gx - i0, fy = gy - j0;
    var a = field[j0 * n + i0], b = field[j0 * n + i1], c = field[j1 * n + i0], d = field[j1 * n + i1];
    return (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy;
  }

  function percentile(sorted, p) {
    if (!sorted.length) {
      return null;
    }
    var index = (sorted.length - 1) * p;
    var lo = Math.floor(index), hi = Math.ceil(index);
    return sorted[lo] + (sorted[hi] - sorted[lo]) * (index - lo);
  }

  // Ensemble members of one variable from an Open-Meteo hourly block, whatever the key suffixes
  // (precipitation, precipitation_member01, precipitation_member01_<model>...).
  function ensembleMembers(hourly, variable) {
    return Object.keys(hourly)
      .filter(function (key) { return key === variable || key.indexOf(variable + "_") === 0; })
      .map(function (key) { return hourly[key]; })
      .filter(function (series) { return Array.isArray(series) && series.some(function (v) { return v !== null; }); });
  }

  function ensembleBand(members) {
    var length = members.length ? members[0].length : 0;
    var p10 = [], p50 = [], p90 = [];
    for (var t = 0; t < length; t++) {
      var values = members.map(function (m) { return m[t]; })
        .filter(function (v) { return v !== null && isFinite(v); })
        .sort(function (a, b) { return a - b; });
      p10.push(percentile(values, 0.1));
      p50.push(percentile(values, 0.5));
      p90.push(percentile(values, 0.9));
    }
    return { p10: p10, p50: p50, p90: p90, count: members.length };
  }

  // Sum of each member over [t0, t1] (ms), then the 10th, 50th and 90th percentiles of those sums.
  function ensembleTotals(times, members, t0, t1) {
    var sums = members.map(function (m) {
      var s = 0;
      for (var t = 0; t < times.length; t++) {
        if (times[t] > t0 && times[t] <= t1 && m[t] !== null && isFinite(m[t])) {
          s += m[t];
        }
      }
      return s;
    }).sort(function (a, b) { return a - b; });
    return { p10: percentile(sums, 0.1), p50: percentile(sums, 0.5), p90: percentile(sums, 0.9) };
  }

  // Sum of values whose time is in (t0, t1].
  function windowSum(times, values, t0, t1) {
    var s = 0, n = 0;
    for (var t = 0; t < times.length; t++) {
      if (times[t] > t0 && times[t] <= t1 && values[t] !== null && isFinite(values[t])) {
        s += values[t];
        n++;
      }
    }
    return n ? s : null;
  }

  // Yearly statistics from daily values: total, days at or above heavyMm, mean. A year counts
  // only with at least 360 valid days, so a partial year never looks like a dry one.
  function yearlyStats(days, values, heavyMm) {
    var years = {};
    for (var d = 0; d < days.length; d++) {
      var v = values[d];
      if (v === null || !isFinite(v)) {
        continue;
      }
      var year = Number(String(days[d]).slice(0, 4));
      var y = years[year] || (years[year] = { total: 0, heavy: 0, count: 0 });
      y.total += v;
      y.count++;
      if (heavyMm !== null && v >= heavyMm) {
        y.heavy++;
      }
    }
    var result = {};
    Object.keys(years).forEach(function (year) {
      var y = years[year];
      if (y.count >= 360) {
        result[year] = { total: y.total, heavy: y.heavy, mean: y.total / y.count };
      }
    });
    return result;
  }

  function periodMean(yearly, field, from, to) {
    var s = 0, n = 0;
    for (var year = from; year <= to; year++) {
      if (yearly[year]) {
        s += yearly[year][field];
        n++;
      }
    }
    return n ? s / n : null;
  }

  function rollingMean(years, values, window) {
    var half = Math.floor(window / 2);
    return values.map(function (_, i) {
      var s = 0, n = 0;
      for (var k = i - half; k <= i + half; k++) {
        if (k >= 0 && k < values.length && values[k] !== null) {
          s += values[k];
          n++;
        }
      }
      return n >= Math.ceil(window * 0.7) ? s / n : null;
    });
  }

  // Minutes to add to UTC to get local time in timeZone, at that instant.
  function tzOffsetMinutes(ms, timeZone) {
    var parts = new Intl.DateTimeFormat("en-GB", {
      timeZone: timeZone, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit"
    }).formatToParts(new Date(ms));
    var get = function (type) { return Number(parts.find(function (p) { return p.type === type; }).value); };
    var asUtc = Date.UTC(get("year"), get("month") - 1, get("day"), get("hour"), get("minute"), get("second"));
    return Math.round((asUtc - Math.floor(ms / 1000) * 1000) / 60000);
  }

  // ---- periods of days ("2021-07-13" strings; calendar arithmetic, no time zone involved) -------
  function addDays(date, days) {
    var d = new Date(date + "T00:00:00Z");
    d.setUTCDate(d.getUTCDate() + days);
    return d.toISOString().slice(0, 10);
  }

  // Number of days of a period, both ends included: from 13 to 16 July is 4.
  function daysInPeriod(from, to) {
    return Math.round((Date.parse(to + "T00:00:00Z") - Date.parse(from + "T00:00:00Z")) / 86400000) + 1;
  }

  function daysOfPeriod(from, to) {
    var days = [];
    for (var n = daysInPeriod(from, to), i = 0; i < n; i++) {
      days.push(addDays(from, i));
    }
    return days;
  }

  // A period as asked, made valid: ends in order, inside [first, last], at most maxDays long
  // (the end is brought closer; "shortened" says so).
  function clampPeriod(from, to, first, last, maxDays) {
    if (!to || to < from) {
      to = from;
    }
    if (from < first) { from = first; }
    if (from > last) { from = last; }
    if (to > last) { to = last; }
    if (to < from) { to = from; }
    var shortened = false;
    if (daysInPeriod(from, to) > maxDays) {
      to = addDays(from, maxDays - 1);
      shortened = true;
    }
    return { from: from, to: to, days: daysInPeriod(from, to), shortened: shortened };
  }

  // Step first offered for a period of this many days, in minutes: the longer the period, the
  // coarser the step, so that Play crosses it in a reasonable time.
  function stepForDays(days) {
    return days <= 3 ? 60 : days <= 10 ? 180 : days <= 31 ? 360 : 1440;
  }

  // Width of a rain bar of the chart, in hours: the step itself when each step gets at least
  // minPx pixels, otherwise the first of 1, 3, 6, 12 or 24 hours that does (24 at the most).
  function barHours(spanHours, stepHours, widthPx, minPx) {
    var choices = [stepHours, 1, 3, 6, 12, 24].filter(function (h) { return h >= stepHours; });
    for (var i = 0; i < choices.length; i++) {
      if (widthPx / Math.max(1, spanHours / choices[i]) >= minPx) {
        return choices[i];
      }
    }
    return 24;
  }

  // A local wall-clock time stored with a "Z" (as the gauge collector does) -> real UTC ms.
  function wallClockToUtc(isoWithZ, timeZone) {
    var wall = Date.parse(isoWithZ);
    var guess = wall - tzOffsetMinutes(wall, timeZone) * 60000;
    return wall - tzOffsetMinutes(guess, timeZone) * 60000;
  }

  function localDateString(ms, timeZone) {
    var parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: timeZone, year: "numeric", month: "2-digit", day: "2-digit"
    }).formatToParts(new Date(ms));
    var get = function (type) { return parts.find(function (p) { return p.type === type; }).value; };
    return get("year") + "-" + get("month") + "-" + get("day");
  }

  function hexToRgb(hex) {
    return [1, 3, 5].map(function (i) { return parseInt(hex.slice(i, i + 2), 16); });
  }

  // [r, g, b, a] of a value for a layer (a = 0 for no rain and for missing values).
  function colourFor(layer, value) {
    if (value === null || !isFinite(value)) {
      return [0, 0, 0, 0];
    }
    if (layer.classes) {
      var cls = null;
      for (var c = 0; c < layer.classes.length; c++) {
        if (value >= layer.classes[c][0]) {
          cls = layer.classes[c];
        }
      }
      return cls ? hexToRgb(cls[1]).concat(Math.round(255 * (cls[2] === undefined ? 1 : cls[2]))) : [0, 0, 0, 0];
    }
    var ramp = layer.ramp;
    if (value <= ramp[0][0]) {
      return hexToRgb(ramp[0][1]).concat(255);
    }
    for (var r = 1; r < ramp.length; r++) {
      if (value <= ramp[r][0]) {
        var f = (value - ramp[r - 1][0]) / (ramp[r][0] - ramp[r - 1][0]);
        var a = hexToRgb(ramp[r - 1][1]), b = hexToRgb(ramp[r][1]);
        return [0, 1, 2].map(function (k) { return Math.round(a[k] + f * (b[k] - a[k])); }).concat(255);
      }
    }
    return hexToRgb(ramp[ramp.length - 1][1]).concat(255);
  }

  // Wind towards which the air moves, from meteorological speed and direction ("from").
  function windComponents(speed, directionDeg) {
    if (speed === null || directionDeg === null || !isFinite(speed) || !isFinite(directionDeg)) {
      return [null, null];
    }
    return [-speed * Math.sin(directionDeg * DEG), -speed * Math.cos(directionDeg * DEG)];
  }

  // ---- a river replay over several days ----------------------------------------------------------
  // Luiz's view files replay one day (".../water-surface.czml?date=2021-07-14"), but the data
  // behind them also takes a period (start and end, the form his own endpoints document). A
  // one-day view becomes a view of the period: in every layer whose address carries date=<day>,
  // the date is replaced by the period (and the replay speed), and the layer gets its own id, so
  // Terria does not take it for the one-day layer it may already hold. The other layers (names,
  // shading) are untouched. Returns a new view; the one given is not changed.
  function periodView(view, day, from, to, multiplier) {
    var copy = JSON.parse(JSON.stringify(view));
    var renamed = {};
    var dateArg = new RegExp("([?&])date=" + day + "(?=&|$)");
    (copy.catalog || []).forEach(function (member) {
      if (typeof member.url !== "string" || !dateArg.test(member.url)) {
        return;
      }
      var period = "start=" + encodeURIComponent(from + "T00:00:00Z") + "&end=" + encodeURIComponent(to + "T23:59:59Z") +
        (multiplier ? "&multiplier=" + multiplier : "");
      member.url = member.url.replace(dateArg, "$1" + period);
      var id = member.id + "-" + from.replace(/-/g, "") + "-" + to.replace(/-/g, "");
      renamed[member.id] = id;
      member.id = id;
    });
    ["workbench", "timeline"].forEach(function (list) {
      if (Array.isArray(copy[list])) {
        copy[list] = copy[list].map(function (id) { return renamed[id] || id; });
      }
    });
    return copy;
  }

  // A river view without its slow layers. Terria puts a view on the map only once every layer of it
  // has loaded, and the water and building layers of a replay are built by the backend first (a
  // minute or more for a new period): until then the map kept the previous view and its timeline.
  // This view replaces them by a timeline-only layer (inline CZML: a clock over the days, nothing
  // to draw), so it loads at once: the map timeline is on the days chosen straight away and the
  // rain follows it, while the full view is still being built. The other layers (names, shading)
  // are kept. from, to: first and last day ("2021-07-14"); times are the gauges' wall-clock times
  // labelled UTC, as in Luiz's replays.
  function clockView(view, from, to, multiplier, label) {
    var copy = JSON.parse(JSON.stringify(view || {}));
    var slow = /\/(water-surface|buildings-3d)\.czml/;
    var removed = {};
    copy.catalog = (copy.catalog || []).filter(function (member) {
      if (typeof member.url === "string" && slow.test(member.url)) {
        removed[member.id] = true;
        return false;
      }
      return true;
    });
    var start = from + "T00:00:00Z", stop = addDays(to, 1) + "T00:00:00Z";
    var id = "wx-river-clock-" + from + "-" + to;
    copy.catalog.push({
      id: id, type: "czml", name: label, isPaused: false,
      czmlData: [{ id: "document", name: label, version: "1.0",
        clock: { interval: start + "/" + stop, currentTime: start, multiplier: multiplier, range: "LOOP_STOP", step: "SYSTEM_CLOCK_MULTIPLIER" } }]
    });
    copy.workbench = [id].concat((copy.workbench || []).filter(function (item) { return !removed[item]; }));
    copy.timeline = [id];
    return copy;
  }

  // ---- wind streamlines -------------------------------------------------------------------------
  // A vector (u east, v north) turned clockwise by turnDeg, as wind directions are counted, and
  // scaled: the 10 m wind of a place becomes the wind higher up, which blows from another direction
  // and faster.
  function turnVector(u, v, turnDeg, scale) {
    var a = turnDeg * DEG;
    return [(u * Math.cos(a) + v * Math.sin(a)) * scale, (-u * Math.sin(a) + v * Math.cos(a)) * scale];
  }

  // The line a particle would follow through a steady wind: from the point given, upstream to
  // where the wind enters the box and downstream to where it leaves it. sample(lon, lat) gives
  // the wind [u, v] in m/s, or null outside the data. Points go from upstream to downstream, each
  // {lon, lat, speed}; stepM is the distance between two points. Each step uses the wind halfway
  // along it (midpoint rule), so curved flow stays smooth.
  function traceStreamline(sample, bbox, lon, lat, stepM, maxSteps) {
    var inside = function (x, y) { return x >= bbox.west && x <= bbox.east && y >= bbox.south && y <= bbox.north; };
    var unit = function (x, y) {
      var w = sample(x, y);
      if (!w || !isFinite(w[0]) || !isFinite(w[1])) {
        return null;
      }
      var speed = Math.sqrt(w[0] * w[0] + w[1] * w[1]);
      return speed < 0.05 ? null : { u: w[0] / speed, v: w[1] / speed, speed: speed };
    };
    var move = function (x, y, d, sign) {
      return [x + sign * d.u * stepM / (EARTH_R * Math.cos(y * DEG)) / DEG, y + sign * d.v * stepM / EARTH_R / DEG];
    };
    var walk = function (sign) {
      var points = [], x = lon, y = lat;
      for (var i = 0; i < maxSteps; i++) {
        var here = unit(x, y);
        if (!here) {
          break;
        }
        var half = move(x, y, here, sign * 0.5);
        var mid = unit(half[0], half[1]) || here;
        var next = move(x, y, mid, sign);
        if (!inside(next[0], next[1])) {
          break;
        }
        x = next[0];
        y = next[1];
        points.push({ lon: x, lat: y, speed: (unit(x, y) || mid).speed });
      }
      return points;
    };
    var start = unit(lon, lat);
    if (!start || !inside(lon, lat)) {
      return [];
    }
    return walk(-1).reverse().concat([{ lon: lon, lat: lat, speed: start.speed }], walk(1));
  }

  // Starting points of count streamlines, evenly spread across the flow: on the line through
  // the centre of the box at right angles to the mean wind (u, v). shift (0 to 1) moves them
  // by part of a spacing, so the lines of two heights do not lie on top of each other.
  function streamSeeds(bbox, u, v, count, shift) {
    var speed = Math.sqrt(u * u + v * v);
    if (!(speed > 0) || count < 1) {
      return [];
    }
    var lat0 = (bbox.south + bbox.north) / 2, lon0 = (bbox.west + bbox.east) / 2;
    var widthM = (bbox.east - bbox.west) * DEG * EARTH_R * Math.cos(lat0 * DEG);
    var heightM = (bbox.north - bbox.south) * DEG * EARTH_R;
    // Unit vector across the flow, and the extent of the box along it.
    var px = -v / speed, py = u / speed;
    var across = (Math.abs(px) * widthM + Math.abs(py) * heightM) * 0.94;
    var seeds = [];
    for (var k = 0; k < count; k++) {
      var offset = ((k + 0.5 + (shift || 0)) / count - 0.5) * across;
      if (Math.abs(offset) > across / 2) {
        continue;
      }
      seeds.push({
        lon: lon0 + px * offset / (EARTH_R * Math.cos(lat0 * DEG)) / DEG,
        lat: lat0 + py * offset / EARTH_R / DEG
      });
    }
    return seeds;
  }

  // Colour of a wind speed (m/s): light blue for slow, blue for moderate, purple for fast.
  var WIND_RAMP = [[0, "#7fdcf5"], [3, "#35c7f0"], [6, "#3f7df2"], [10, "#8b3ff0"], [16, "#c23ff0"]];

  function windColour(speed) {
    if (speed === null || !isFinite(speed) || speed <= 0) {
      return "rgb(" + hexToRgb(WIND_RAMP[0][1]).join(",") + ")";
    }
    for (var i = 1; i < WIND_RAMP.length; i++) {
      if (speed <= WIND_RAMP[i][0]) {
        var a = hexToRgb(WIND_RAMP[i - 1][1]), b = hexToRgb(WIND_RAMP[i][1]);
        var f = (speed - WIND_RAMP[i - 1][0]) / (WIND_RAMP[i][0] - WIND_RAMP[i - 1][0]);
        return "rgb(" + [0, 1, 2].map(function (c) { return Math.round(a[c] + (b[c] - a[c]) * f); }).join(",") + ")";
      }
    }
    return "rgb(" + hexToRgb(WIND_RAMP[WIND_RAMP.length - 1][1]).join(",") + ")";
  }

  // Open-Meteo returns an object for one location and a list for several.
  function asList(json) {
    return Array.isArray(json) ? json : [json];
  }

  // Hourly blocks of several locations -> { times (ms), values: { variable: [time][point] } }.
  function framesFromLocations(locations, variables) {
    var first = locations[0].hourly;
    var times = first.time.map(function (s) { return s * 1000; });
    var values = {};
    variables.forEach(function (variable) {
      values[variable] = times.map(function (_, t) {
        return locations.map(function (loc) {
          var series = loc.hourly[variable];
          return series ? series[t] : null;
        });
      });
    });
    return { times: times, values: values };
  }

  function indexAtOrBefore(times, ms) {
    var index = -1;
    for (var t = 0; t < times.length; t++) {
      if (times[t] <= ms) {
        index = t;
      }
    }
    return index;
  }

  // Look of the rain from the hourly rate (mm/h); null below 0.1 mm/h.
  function rainLook(mmPerHour) {
    if (!(mmPerHour >= 0.1)) {
      return null;
    }
    // 25 mm/h and more gets the strongest look.
    var r = Math.min(1, Math.log(1 + mmPerHour) / Math.log(26));
    return {
      density: 0.3 + 0.65 * r,
      size: 0.7 + 0.9 * r,
      speed: 0.6 + 0.7 * r,
      haze: 0.02 + 0.2 * r,
      fallMs: 3 + 6 * r
    };
  }

  function nearestIndex(times, ms) {
    var best = -1, bestDiff = Infinity;
    for (var t = 0; t < times.length; t++) {
      var diff = Math.abs(times[t] - ms);
      if (diff < bestDiff) {
        best = t;
        bestDiff = diff;
      }
    }
    return best;
  }

  // 15-minute frames: rain from the 15-minute data (mm per 15 minutes, turned into mm/h so the
  // map, the legend and the falling rain read the same as in hourly mode); every other variable
  // keeps the value of the nearest hour, because only rain is given every 15 minutes.
  function quarterHourFrames(hourly, quarterTimes, quarterRain) {
    var values = {};
    Object.keys(hourly.values).forEach(function (variable) {
      values[variable] = quarterTimes.map(function (ms, q) {
        if (variable === "precipitation") {
          return quarterRain[q].map(function (v) { return v === null || v === undefined ? null : v * 4; });
        }
        var h = nearestIndex(hourly.times, ms);
        return h < 0 ? quarterRain[q].map(function () { return null; }) : hourly.values[variable][h].slice();
      });
    });
    return { times: quarterTimes.slice(), values: values, stepHours: 0.25 };
  }

  var CORE = {
    CONFIG: CONFIG, LAYERS: LAYERS, expandBbox: expandBbox, lattice: lattice, outerRings: outerRings,
    bboxOfCoordinates: bboxOfCoordinates, pointInRings: pointInRings, distanceM: distanceM,
    idwMatrix: idwMatrix, areaMask: areaMask, areaWeights: areaWeights, fieldFrom: fieldFrom,
    weightedMean: weightedMean, sampleField: sampleField, percentile: percentile,
    ensembleMembers: ensembleMembers, ensembleBand: ensembleBand, ensembleTotals: ensembleTotals,
    windowSum: windowSum, yearlyStats: yearlyStats, periodMean: periodMean, rollingMean: rollingMean,
    tzOffsetMinutes: tzOffsetMinutes, wallClockToUtc: wallClockToUtc, localDateString: localDateString,
    colourFor: colourFor, windComponents: windComponents, asList: asList,
    framesFromLocations: framesFromLocations, indexAtOrBefore: indexAtOrBefore, rainLook: rainLook, quarterHourFrames: quarterHourFrames,
    addDays: addDays, daysInPeriod: daysInPeriod, daysOfPeriod: daysOfPeriod, clampPeriod: clampPeriod,
    stepForDays: stepForDays, barHours: barHours,
    turnVector: turnVector, traceStreamline: traceStreamline, streamSeeds: streamSeeds, windColour: windColour,
    periodView: periodView, clockView: clockView
  };
  root.WeatherTwinCore = CORE;

  // Under node (tests) there is no page: stop here.
  if (typeof document === "undefined") {
    if (typeof module !== "undefined" && module.exports) {
      module.exports = CORE;
    }
    return;
  }

  // =============================================================================================
  // PROVIDERS: the only place that fetches data. Replace these with the eddie_weather API later.
  // =============================================================================================

  function fetchJson(url, timeoutMs) {
    var controller = typeof AbortController !== "undefined" ? new AbortController() : null;
    var timer = controller ? setTimeout(function () { controller.abort(); }, timeoutMs || 45000) : null;
    return fetch(url, controller ? { signal: controller.signal } : {})
      .then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (body) {
          if (!response.ok) {
            var reason = body && (body.reason || body.message) ? body.reason || body.message : "HTTP " + response.status;
            var error = new Error(reason);
            error.status = response.status;
            throw error;
          }
          return body;
        });
      })
      .finally(function () {
        if (timer) {
          clearTimeout(timer);
        }
      });
  }

  function query(params) {
    return Object.keys(params).filter(function (k) { return params[k] !== undefined && params[k] !== null; })
      .map(function (k) { return encodeURIComponent(k) + "=" + encodeURIComponent(params[k]); }).join("&");
  }

  function coordinateParams(points) {
    return {
      latitude: points.map(function (p) { return p.lat.toFixed(4); }).join(","),
      longitude: points.map(function (p) { return p.lon.toFixed(4); }).join(",")
    };
  }

  function cacheGet(key) {
    try {
      var raw = localStorage.getItem(CONFIG.cacheVersion + ":" + key);
      return raw ? JSON.parse(raw) : null;
    } catch (e) {
      return null;
    }
  }

  function cacheSet(key, value) {
    try {
      localStorage.setItem(CONFIG.cacheVersion + ":" + key, JSON.stringify(value));
    } catch (e) {
      // Storage full or unavailable: the data is simply fetched again next time.
    }
  }

  var PROVIDERS = {
    // ---- area: Robin's AOI module ------------------------------------------------------------
    area: function (reachId) {
      return fetchJson(CONFIG.backendUrl + "/aoi/reach/" + encodeURIComponent(reachId) + "/variants.geojson", 20000)
        .then(function (collection) { return chooseArea(reachId, collection); });
    },

    // ---- gridded weather: Open-Meteo ---------------------------------------------------------
    // kind: "forecast" (pastDays, forecastDays) or "archive" (date, and endDate for a period; local days).
    grid: function (points, request) {
      var params = Object.assign(coordinateParams(points), {
        hourly: CONFIG.hourlyVariables.join(","),
        wind_speed_unit: "ms",
        timeformat: "unixtime",
        timezone: CONFIG.timeZone
      });
      var url;
      if (request.kind === "archive") {
        params.start_date = request.date;
        params.end_date = request.endDate || request.date;
        url = CONFIG.openMeteo.archive;
      } else {
        params.past_days = request.pastDays || 0;
        params.forecast_days = request.forecastDays || 1;
        url = CONFIG.openMeteo.forecast;
      }
      var hourlyOnly = function () {
        delete params.minutely_15;
        return fetchJson(url + "?" + query(params), 60000);
      };
      if (request.quarterHour) {
        params.minutely_15 = "precipitation";
      }
      var answer = request.quarterHour ? fetchJson(url + "?" + query(params), 60000).catch(hourlyOnly) : hourlyOnly();
      return answer.then(function (json) {
        var locations = asList(json);
        var frames = framesFromLocations(locations, CONFIG.hourlyVariables);
        frames.stepHours = 1;
        frames.hourly = { times: frames.times, values: frames.values };
        var quarter = locations[0].minutely_15;
        // 15-minute rain only when the source really has it for this place.
        if (request.quarterHour && quarter && quarter.time && quarter.precipitation &&
            quarter.precipitation.some(function (v) { return v !== null; })) {
          var quarterTimes = quarter.time.map(function (sec) { return sec * 1000; });
          var quarterRain = quarterTimes.map(function (_, q) {
            return locations.map(function (loc) { return loc.minutely_15 ? loc.minutely_15.precipitation[q] : null; });
          });
          var q = quarterHourFrames(frames, quarterTimes, quarterRain);
          q.hourly = frames.hourly;
          frames = q;
        }
        frames.source = request.kind === "archive" ?
          "Open-Meteo archive (ERA5 / ERA5-Land reanalysis)" : "Open-Meteo forecast (weather model)";
        return frames;
      });
    },

    // Wind above 10 m, at one point (the centre of the area: at these heights the wind hardly
    // differs across 15 km). Same periods as the gridded weather.
    windAloft: function (point, request) {
      var archive = request.kind === "archive";
      var heights = archive ? CONFIG.windHeightsArchive : CONFIG.windHeightsForecast;
      var names = [];
      heights.forEach(function (h) { names.push("wind_speed_" + h + "m", "wind_direction_" + h + "m"); });
      var params = {
        latitude: point.lat.toFixed(4), longitude: point.lon.toFixed(4), hourly: names.join(","),
        wind_speed_unit: "ms", timeformat: "unixtime", timezone: CONFIG.timeZone
      };
      if (archive) {
        params.start_date = request.date;
        params.end_date = request.endDate || request.date;
      } else {
        params.past_days = request.pastDays || 0;
        params.forecast_days = request.forecastDays || 1;
      }
      return fetchJson((archive ? CONFIG.openMeteo.archive : CONFIG.openMeteo.forecast) + "?" + query(params), 45000).then(function (json) {
        var hourly = asList(json)[0].hourly;
        var aloft = { times: hourly.time.map(function (sec) { return sec * 1000; }), heights: [], speed: {}, direction: {} };
        heights.forEach(function (h) {
          var speed = hourly["wind_speed_" + h + "m"], direction = hourly["wind_direction_" + h + "m"];
          if (speed && direction && speed.some(function (v) { return v !== null; })) {
            aloft.heights.push(h);
            aloft.speed[h] = speed;
            aloft.direction[h] = direction;
          }
        });
        return aloft;
      });
    },

    ensemble: function (point, days) {
      var models = CONFIG.ensembleModels.slice();
      function attempt() {
        if (!models.length) {
          return Promise.reject(new Error("No ensemble model answered."));
        }
        var model = models.shift();
        var params = {
          latitude: point.lat.toFixed(4), longitude: point.lon.toFixed(4), hourly: "precipitation",
          models: model, forecast_days: days, timeformat: "unixtime", timezone: CONFIG.timeZone
        };
        return fetchJson(CONFIG.openMeteo.ensemble + "?" + query(params), 60000).then(function (json) {
          var hourly = asList(json)[0].hourly;
          var members = ensembleMembers(hourly, "precipitation");
          if (members.length < 5) {
            throw new Error("Too few members from " + model);
          }
          return {
            model: model, times: hourly.time.map(function (s) { return s * 1000; }),
            members: members, band: ensembleBand(members)
          };
        }).catch(function (error) {
          return models.length ? attempt() : Promise.reject(error);
        });
      }
      return attempt();
    },

    climate: function (point) {
      var key = "climate:" + point.lat.toFixed(2) + "," + point.lon.toFixed(2) + ":" + CONFIG.climateModels.join(",");
      var cached = cacheGet(key);
      if (cached) {
        return Promise.resolve(cached);
      }
      var params = {
        latitude: point.lat.toFixed(4), longitude: point.lon.toFixed(4),
        start_date: "1950-01-01", end_date: "2049-12-31",
        models: CONFIG.climateModels.join(","), daily: "precipitation_sum,temperature_2m_mean"
      };
      return fetchJson(CONFIG.openMeteo.climate + "?" + query(params), 120000).then(function (json) {
        var daily = asList(json)[0].daily;
        var models = {};
        CONFIG.climateModels.forEach(function (model) {
          var precip = daily["precipitation_sum_" + model] || (CONFIG.climateModels.length === 1 ? daily.precipitation_sum : null);
          var temp = daily["temperature_2m_mean_" + model] || (CONFIG.climateModels.length === 1 ? daily.temperature_2m_mean : null);
          if (!precip) {
            return;
          }
          var p = yearlyStats(daily.time, precip, CONFIG.heavyRainMm);
          var t = temp ? yearlyStats(daily.time, temp, null) : {};
          models[model] = {};
          Object.keys(p).forEach(function (year) {
            models[model][year] = { total: p[year].total, heavy: p[year].heavy, temp: t[year] ? t[year].mean : null };
          });
        });
        var result = { models: models };
        cacheSet(key, result);
        return result;
      });
    },

    // Reanalysis yearly values since 1950 at one point, to compare the climate models with.
    reanalysisYears: function (point) {
      var lastYear = new Date().getUTCFullYear() - 1;
      var key = "era5years:" + point.lat.toFixed(2) + "," + point.lon.toFixed(2) + ":" + lastYear;
      var cached = cacheGet(key);
      if (cached) {
        return Promise.resolve(cached);
      }
      var params = {
        latitude: point.lat.toFixed(4), longitude: point.lon.toFixed(4),
        start_date: "1950-01-01", end_date: lastYear + "-12-31",
        daily: "precipitation_sum,temperature_2m_mean", timezone: CONFIG.timeZone
      };
      return fetchJson(CONFIG.openMeteo.archive + "?" + query(params), 120000).then(function (json) {
        var daily = asList(json)[0].daily;
        var p = yearlyStats(daily.time, daily.precipitation_sum, CONFIG.heavyRainMm);
        var t = yearlyStats(daily.time, daily.temperature_2m_mean, null);
        var years = {};
        Object.keys(p).forEach(function (year) {
          years[year] = { total: p[year].total, heavy: p[year].heavy, temp: t[year] ? t[year].mean : null };
        });
        cacheSet(key, years);
        return years;
      });
    },

    // ---- water level: Luiz's endpoints -------------------------------------------------------
    waterDay: function (date) {
      return fetchJson(CONFIG.backendUrl + "/water-level/day/" + date + ".json", 20000).then(function (data) {
        var times = data.times.map(function (s) {
          return CONFIG.waterTimesAreLocal ? wallClockToUtc(s, CONFIG.timeZone) : Date.parse(s);
        });
        var metres = function (series) { return (series || []).map(function (v) { return v === null ? null : v / 100; }); };
        return { times: times, hesperange: metres(data.hesperange_cm), pfaffenthal: metres(data.pfaffenthal_cm), livange: metres(data.livange_cm) };
      });
    },

    waterLatest: function () {
      return fetchJson(CONFIG.backendUrl + "/water-level/latest", 20000);
    },

    gauges: function () {
      return fetchJson(CONFIG.backendUrl + "/water-level/gauges.json", 20000).then(function (data) { return data.gauges || []; });
    },

    // ---- observed rain: ASTA stations, when the browser is allowed to reach them -------------
    stations: function () {
      return fetchJson(CONFIG.astaLiveUrl, 20000).then(function (json) {
        var latest = {};
        (json.features || []).forEach(function (feature) {
          var p = feature.properties || {};
          var value = Number(p.sum_nn050);
          if (!feature.geometry || !isFinite(value)) {
            return;
          }
          var id = String(p.featureofinterest_xlink_href || p.name_descr || "").split("/").pop();
          var time = Date.parse(p.datetime);
          if (!latest[id] || time > latest[id].time) {
            latest[id] = {
              id: id, name: p.name_descr || id, value: value, time: time,
              lon: feature.geometry.coordinates[0], lat: feature.geometry.coordinates[1]
            };
          }
        });
        return Object.keys(latest).map(function (id) { return latest[id]; });
      });
    }
  };

  // "1000 m buffer", "300 m corridor", "watersheds"... from a variant's name and parameters.
  function variantLabel(props) {
    var name = props.variant || props.variant_key || "area";
    var params = props.params;
    if (typeof params === "string") {
      try {
        params = JSON.parse(params);
      } catch (e) {
        params = null;
      }
    }
    var metres = params ? params.d || params.half_width_m || params.margin_m : null;
    var area = props.area_km2 ? ", " + Number(props.area_km2).toFixed(1) + " km²" : "";
    return (metres ? metres + " m " : "") + name + area;
  }

  // Choose the averaging area among a reach's variants, by CONFIG.areaVariantPreference.
  function chooseArea(reachId, collection) {
    var features = (collection && collection.features) || [];
    var polygons = features.filter(function (f) { return f.geometry && /Polygon/.test(f.geometry.type); });
    var chosen = null;
    CONFIG.areaVariantPreference.some(function (name) {
      chosen = polygons.find(function (f) {
        var props = f.properties || {};
        return props.requested_variant === name || props.variant === name || String(props.variant_key || "").indexOf(name) === 0;
      });
      return Boolean(chosen);
    });
    if (!chosen && polygons.length) {
      chosen = polygons[0];
    }
    var centerline = features.find(function (f) { return (f.properties || {}).variant === "centerline"; });
    var geometry = chosen ? chosen.geometry : null;
    var bbox = geometry ? bboxOfCoordinates(geometry.coordinates) :
      centerline ? bboxOfCoordinates(centerline.geometry.coordinates) : null;
    if (!bbox) {
      throw new Error("The reach " + reachId + " has no geometry yet.");
    }
    var props = chosen ? chosen.properties || {} : {};
    return {
      reachId: reachId,
      geometry: geometry,
      rings: outerRings(geometry),
      bbox: bbox,
      // The variant actually built, not the one requested: a degraded "watersheds" entry is a buffer.
      variant: chosen ? variantLabel(props) : "centerline bounding box",
      requested: props.requested_variant || props.variant || null,
      degraded: Boolean(props.degraded),
      reason: props.degraded_reason || null
    };
  }

  // =============================================================================================
  // TERRIA AND CESIUM ACCESS
  // =============================================================================================

  var terria = null;

  // Terria keeps its state in React; find it through the React tree (same approach as
  // water-level-control.js). Isolated here so it can be replaced in one place.
  function findTerria() {
    if (terria) {
      return terria;
    }
    var nodes = document.querySelectorAll("body *");
    for (var i = 0; i < nodes.length && !terria; i++) {
      var key = Object.keys(nodes[i]).find(function (k) {
        return k.indexOf("__reactInternalInstance$") === 0 || k.indexOf("__reactFiber$") === 0;
      });
      for (var fiber = key && nodes[i][key]; fiber && !terria; fiber = fiber["return"]) {
        var props = fiber.memoizedProps;
        if (props && props.terria && props.terria.timelineClock) {
          terria = props.terria;
        } else if (props && props.viewState && props.viewState.terria) {
          terria = props.viewState.terria;
        }
      }
    }
    return terria;
  }

  function scene() {
    var t = findTerria();
    return t && t.cesium && t.cesium.scene ? t.cesium.scene : null;
  }

  // Terria stops drawing the 3D scene when nothing moves: camera still for a second, no terrain
  // tile loading, map timeline not playing. Cesium's own requestRender() does not start it again;
  // only Terria's notifyRepaintRequired() does. Without it, a change made by this script (rain
  // starting or stopping, a new weather field) stayed invisible until the mouse moved over the
  // map or the timeline played: the rain looked late, or frozen. So every change asks Terria to
  // draw, and while it rains this is asked on every frame.
  function repaint(s) {
    var t = findTerria();
    try {
      if (t && t.currentViewer && typeof t.currentViewer.notifyRepaintRequired === "function") {
        t.currentViewer.notifyRepaintRequired();
      }
    } catch (e) {
      // Drawing is then left to Cesium's own request below.
    }
    s = s || scene();
    if (s && s.requestRender) {
      s.requestRender();
    }
  }

  // Cesium classes taken from objects that already exist in the scene, not from the minified
  // bundle: sturdier across Terria updates.
  function cesiumKit(s) {
    if (!s || !s.imageryLayers || !s.imageryLayers.length) {
      return null;
    }
    var baseProvider = s.imageryLayers.get(0).imageryProvider;
    if (!baseProvider || !baseProvider.tilingScheme) {
      return null;
    }
    var tilingScheme = baseProvider.tilingScheme;
    var fxaa = s.postProcessStages && s.postProcessStages.fxaa;
    return {
      tilingScheme: tilingScheme,
      Rectangle: tilingScheme.rectangle.constructor,
      Event: baseProvider.errorEvent ? baseProvider.errorEvent.constructor : null,
      Cartesian3: s.camera.position.constructor,
      PostProcessStage: fxaa ? fxaa.constructor : null
    };
  }

  // The reach on Terria's workbench, put there by Robin's AOI selector; else the fallback reach.
  function workbenchReachId() {
    var t = findTerria();
    var items = (t && t.workbench && t.workbench.items) || [];
    for (var i = 0; i < items.length; i++) {
      var url = String(items[i].url || "");
      var match = url.match(/\/aoi\/reach\/([A-Za-z0-9_-]+)\/variants\.geojson/);
      if (match) {
        return match[1];
      }
    }
    return CONFIG.fallbackReachId;
  }

  function terriaClockMs() {
    var t = findTerria();
    if (!t || !t.timelineClock) {
      return null;
    }
    var now = t.timelineClock.currentTime;
    var ms = now.constructor.toDate(now).getTime();
    // Luiz's day replay runs the clock in local wall-clock time labelled UTC.
    return CONFIG.waterTimesAreLocal ? wallClockToUtc(new Date(ms).toISOString(), CONFIG.timeZone) : ms;
  }

  // =============================================================================================
  // STATE
  // =============================================================================================

  var state = {
    mode: "now",
    area: null,
    grid: null,          // { points, W, weights, mask, maskCount, bbox, n }
    frames: null,        // { times, values, source }
    window: null,        // [firstIndex, lastIndex] of frames shown on the slider
    areaSeries: null,    // { variable: [area average per time] }
    ensemble: null,
    climate: null,
    reanalysis: null,
    water: null,
    waterLatest: null,
    stations: null,
    stationsNote: "",
    gauges: null,
    index: 0,
    layer: "precipitation",
    wind: true,
    rainEffect: true,
    callouts: true,
    opacity: 0.7,
    stepMin: 60,
    playing: false,
    sync: false,
    historyDate: null,
    historyEnd: null,
    riverKind: null,
    forecastFrom: null,
    forecastTo: null,
    replay: null,
    replayDay: null,
    climateVariable: "total",
    token: 0,
    status: "",
    fieldCache: {}
  };

  // ---- answers already fetched in this visit ---------------------------------------------------
  // Going back to a mode or to a day shows it at once instead of asking the source again, and the
  // forecast is fetched in the background once the live weather is on screen. Failed requests
  // are not kept. Only the wrappers below know about this; the providers stay plain fetches.
  var remembered = {};

  function remember(key, maxAgeMin, fetcher) {
    var entry = remembered[key];
    if (entry && Date.now() - entry.at < maxAgeMin * 60000) {
      return entry.promise;
    }
    entry = { at: Date.now(), promise: fetcher() };
    remembered[key] = entry;
    entry.promise.catch(function () {
      if (remembered[key] === entry) {
        delete remembered[key];
      }
    });
    var keys = Object.keys(remembered);
    if (keys.length > 60) {
      keys.sort(function (a, b) { return remembered[a].at - remembered[b].at; });
      delete remembered[keys[0]];
    }
    return entry.promise;
  }

  (function () {
    var grid = PROVIDERS.grid, ensemble = PROVIDERS.ensemble, waterDay = PROVIDERS.waterDay;
    PROVIDERS.grid = function (points, request) {
      var keep = request.kind === "archive" ? CONFIG.keepPastMin : request.quarterHour ? CONFIG.keepNowMin : CONFIG.keepForecastMin;
      var where = coordinateParams(points);
      return remember("grid|" + JSON.stringify(request) + "|" + where.latitude + "|" + where.longitude, keep,
        function () { return grid(points, request); });
    };
    PROVIDERS.ensemble = function (point, days) {
      return remember("ensemble|" + point.lat.toFixed(4) + "," + point.lon.toFixed(4) + "|" + days, CONFIG.keepForecastMin,
        function () { return ensemble(point, days); });
    };
    var windAloft = PROVIDERS.windAloft;
    PROVIDERS.windAloft = function (point, request) {
      var keep = request.kind === "archive" ? CONFIG.keepPastMin : request.quarterHour ? CONFIG.keepNowMin : CONFIG.keepForecastMin;
      return remember("aloft|" + JSON.stringify(request) + "|" + point.lat.toFixed(4) + "," + point.lon.toFixed(4), keep,
        function () { return windAloft(point, request); });
    };
    // A past day of gauge readings does not change any more; today and yesterday still do.
    PROVIDERS.waterDay = function (date) {
      var recent = date >= localDateString(Date.now() - 86400000, CONFIG.timeZone);
      return remember("water|" + date, recent ? 5 : CONFIG.keepPastMin, function () { return waterDay(date); });
    };
  })();

  // The History period: historyDate is its first day, historyEnd its last (the same for one day).
  function historyEnd() {
    return state.historyEnd && state.historyEnd >= state.historyDate ? state.historyEnd : state.historyDate;
  }

  function historyDays() {
    return daysInPeriod(state.historyDate, historyEnd());
  }

  function periodText() {
    return historyDays() === 1 ? state.historyDate : state.historyDate + " to " + historyEnd() + " (" + historyDays() + " days)";
  }

  // The request each mode sends for the gridded weather (also used to fetch ahead).
  function gridRequest(mode) {
    if (mode === "history") {
      return state.historyEnd && state.historyEnd !== state.historyDate ?
        { kind: "archive", date: state.historyDate, endDate: state.historyEnd } :
        { kind: "archive", date: state.historyDate };
    }
    if (mode === "forecast") {
      return { kind: "forecast", pastDays: 0, forecastDays: CONFIG.forecastDays };
    }
    return { kind: "forecast", pastDays: CONFIG.nowPastDays, forecastDays: 2, quarterHour: true };
  }

  function areaCentre() {
    return { lon: (state.area.bbox.west + state.area.bbox.east) / 2, lat: (state.area.bbox.south + state.area.bbox.north) / 2 };
  }

  // Once the live weather is shown: fetch what the next clicks need, quietly, so that Forecast
  // and "Show the river" answer at once. The 2050 data is not fetched ahead: it is a large
  // download (100 years, four models), kept in the browser after its first use.
  function fetchAhead() {
    if (!state.grid || !state.area) {
      return;
    }
    var quiet = function () { return null; };
    PROVIDERS.grid(state.grid.points, gridRequest("forecast")).catch(quiet);
    PROVIDERS.ensemble(areaCentre(), CONFIG.forecastDays).catch(quiet);
    normalLevels().catch(quiet);
  }

  // =============================================================================================
  // LOADING
  // =============================================================================================

  function setStatus(text, kind) {
    state.status = text;
    if (ui.status) {
      ui.status.textContent = text;
      ui.status.className = "wx-status" + (kind ? " wx-" + kind : "");
    }
  }

  function prepareGrid(area) {
    var dataBbox = expandBbox(area.bbox, CONFIG.areaMarginKm);
    var lat = lattice(dataBbox, CONFIG.latticeSpacingKm, CONFIG.latticeMaxPoints);
    var n = CONFIG.fieldSize;
    var W = idwMatrix(lat.points, dataBbox, n, CONFIG.idwPower);
    var mask = areaMask(area.rings, dataBbox, n);
    return {
      points: lat.points, spacingKm: lat.spacingKm, W: W, n: n, bbox: dataBbox,
      mask: mask.mask, maskCount: mask.count, weights: areaWeights(W, mask.mask, n, lat.points.length)
    };
  }

  function loadArea() {
    var reachId = workbenchReachId();
    if (state.area && state.area.reachId === reachId) {
      return Promise.resolve(state.area);
    }
    setStatus("Loading the area of " + reachId + "…");
    return PROVIDERS.area(reachId).catch(function (error) {
      return fallbackArea(reachId, error.message);
    }).then(function (area) {
      state.area = area;
      state.grid = prepareGrid(area);
      renderAreaNote();
      return area;
    });
  }

  function boxRing(b) {
    return [[b.west, b.south], [b.east, b.south], [b.east, b.north], [b.west, b.north], [b.west, b.south]];
  }

  // Without the AOI module: a box around the three gauges, or a fixed box around km 35-50.
  // Both are named as substitutes in the panel.
  function fallbackArea(reachId, reason) {
    return PROVIDERS.gauges().then(function (gauges) {
      if (!gauges.length) {
        throw new Error("no gauges");
      }
      var box = expandBbox(bboxOfCoordinates(gauges.map(function (g) { return [g.lon, g.lat]; })), 1.5);
      state.gauges = gauges;
      return { reachId: reachId, geometry: null, rings: [boxRing(box)], bbox: box, variant: "box around the gauges",
        requested: "watersheds", degraded: true, reason: "AOI module not reachable: " + reason };
    }).catch(function () {
      var box = { west: 6.10, south: 49.52, east: 6.18, north: 49.63 };
      return { reachId: reachId, geometry: null, rings: [boxRing(box)], bbox: box, variant: "fixed box around km 35–50",
        requested: "watersheds", degraded: true, reason: "AOI module and gauge positions not reachable" };
    });
  }

  function computeAreaSeries() {
    var series = {};
    CONFIG.hourlyVariables.forEach(function (variable) {
      series[variable] = state.frames.values[variable].map(function (values) {
        return weightedMean(state.grid.weights, values);
      });
    });
    state.areaSeries = series;
    // Hourly area rain, for the 24-hour and 7-day totals whatever the step shown.
    var hourly = state.frames.hourly || state.frames;
    state.hourlyRain = {
      times: hourly.times,
      values: hourly.values.precipitation.map(function (values) { return weightedMean(state.grid.weights, values); })
    };
  }

  function load(mode) {
    var token = ++state.token;
    state.mode = mode;
    state.fieldCache = {};
    state.frames = null;
    state.ensemble = null;
    state.water = null;
    state.stations = null;
    state.stationsNote = "";
    state.aloft = null;
    stopPlaying();
    renderModeControls();
    var current = function () { return token === state.token; };
    setBusy(true);

    return loadArea().then(function () {
      if (!current()) {
        return null;
      }
      if (mode === "climate") {
        return loadClimate(current);
      }
      return loadWeather(mode, current);
    }).catch(function (error) {
      if (current()) {
        setStatus("Could not load the weather: " + error.message + ". Check the network and try again.", "error");
        renderAll();
      }
    }).then(function () {
      if (current()) {
        setBusy(false);
      }
    });
  }

  // A moving line under the top bar while something is loading.
  function setBusy(busy) {
    if (panels.bar) {
      panels.bar.classList.toggle("wx-busy", !!busy);
    }
  }

  function loadWeather(mode, current) {
    var now = Date.now();
    var request = gridRequest(mode);
    if (mode === "history") {
      setStatus("Loading " + periodText() + " from the reanalysis archive…");
    } else if (mode === "forecast") {
      setStatus("Loading the " + CONFIG.forecastDays + "-day forecast…");
    } else {
      setStatus("Loading the latest weather…");
    }

    var gridPromise = PROVIDERS.grid(state.grid.points, request);
    var extras = [];

    // Water level for the days shown (gauge records start in December 2017).
    var waterDates = [];
    if (mode === "history") {
      // The gauge line is fetched day by day: for periods up to historyWaterMaxDays only.
      var periodDays = daysOfPeriod(state.historyDate, historyEnd());
      if (periodDays.length <= CONFIG.historyWaterMaxDays) {
        waterDates = periodDays.filter(function (d) { return d >= CONFIG.waterHistoryStart; });
      }
    } else if (mode === "now") {
      waterDates = [localDateString(now - 86400000 * 2, CONFIG.timeZone), localDateString(now - 86400000, CONFIG.timeZone),
        localDateString(now, CONFIG.timeZone)];
    }
    if (waterDates.length) {
      extras.push(Promise.all(waterDates.map(function (d) {
        return PROVIDERS.waterDay(d).catch(function () { return null; });
      })).then(function (days) {
        var merged = { times: [], hesperange: [], pfaffenthal: [], livange: [] };
        days.filter(Boolean).forEach(function (day) {
          ["times", "hesperange", "pfaffenthal", "livange"].forEach(function (k) { merged[k] = merged[k].concat(day[k]); });
        });
        if (current()) {
          state.water = merged.times.length ? merged : null;
        }
      }));
    }
    if (mode === "now") {
      extras.push(PROVIDERS.waterLatest().then(function (latest) {
        if (current()) {
          state.waterLatest = latest;
        }
      }).catch(function () { return null; }));
      extras.push(PROVIDERS.stations().then(function (stations) {
        if (current()) {
          state.stations = stations.filter(function (s) {
            return s.lon >= state.grid.bbox.west && s.lon <= state.grid.bbox.east &&
              s.lat >= state.grid.bbox.south && s.lat <= state.grid.bbox.north;
          });
          state.stationsNote = state.stations.length ? "" : "No ASTA station inside the drawn area.";
        }
      }).catch(function () {
        if (current()) {
          state.stationsNote = "ASTA stations are not reachable from the browser; the map shows the model only.";
        }
      }));
    }
    // Wind higher up, for the streamlines; without it only the 10 m lines are drawn.
    extras.push(PROVIDERS.windAloft(areaCentre(), request).then(function (aloft) {
      if (current()) {
        state.aloft = aloft;
      }
    }).catch(function () { return null; }));
    if (mode === "forecast") {
      extras.push(PROVIDERS.ensemble(areaCentre(), CONFIG.forecastDays).then(function (ensemble) {
        if (current()) {
          state.ensemble = ensemble;
        }
      }).catch(function () { return null; }));
    }

    return Promise.all([gridPromise].concat(extras)).then(function (results) {
      if (!current()) {
        return;
      }
      state.frames = results[0];
      computeAreaSeries();
      setStepOptions();
      var times = state.frames.times;
      if (mode === "now") {
        var first = indexAtOrBefore(times, now - CONFIG.nowWindowPastH * 3600000);
        var nowIndex = Math.max(indexAtOrBefore(times, now), 0);
        var last = indexAtOrBefore(times, now + CONFIG.nowWindowAheadH * 3600000);
        state.window = [Math.max(first, 0), Math.max(last, nowIndex)];
        state.index = nowIndex;
      } else if (mode === "forecast") {
        var start = Math.max(indexAtOrBefore(times, now), 0);
        state.window = [start, times.length - 1];
        var span = forecastSpan();
        if (!span.all) {
          // The days chosen: a day runs from local midnight to the next one, and each time
          // closes the period before it, so the first period of a day ends after midnight.
          var dayStart = wallClockToUtc(span.from + "T00:00:00Z", CONFIG.timeZone);
          var dayEnd = wallClockToUtc(addDays(span.to, 1) + "T00:00:00Z", CONFIG.timeZone);
          var first = indexAtOrBefore(times, dayStart) + 1;
          var last = indexAtOrBefore(times, dayEnd);
          if (last >= first) {
            // Today starts now, not at midnight: the hours already past are not a forecast.
            state.window = [Math.max(first, start), Math.max(last, start)];
          }
        }
        state.index = state.window[0];
      } else {
        state.window = [0, times.length - 1];
        var wettest = 0;
        state.areaSeries.precipitation.forEach(function (v, i) {
          if (v !== null && v > (state.areaSeries.precipitation[wettest] || 0)) {
            wettest = i;
          }
        });
        state.index = wettest;
      }
      var notes = [];
      if (state.frames.values.precipitation.every(function (row) { return row.every(function (v) { return v === null; }); })) {
        notes.push("The source has no data for this period yet.");
      }
      if (mode === "forecast" && !state.ensemble) {
        notes.push("No ensemble forecast answered, so there is no uncertainty band.");
      }
      setStatus(notes.length ? notes.join(" ") : "Loaded. " + state.frames.source + ".", notes.length ? "warn" : "ok");
      renderAll();
    });
  }

  function loadClimate(current) {
    setStatus("Loading climate projections and the reanalysis since 1950 (first time: up to a minute)…");
    var centre = { lon: (state.area.bbox.west + state.area.bbox.east) / 2, lat: (state.area.bbox.south + state.area.bbox.north) / 2 };
    return Promise.all([
      PROVIDERS.climate(centre),
      PROVIDERS.reanalysisYears(centre).catch(function () { return null; })
    ]).then(function (results) {
      if (!current()) {
        return;
      }
      state.climate = results[0];
      state.reanalysis = results[1];
      var count = Object.keys(state.climate.models).length;
      setStatus(count ? "Loaded " + count + " climate models (CMIP6 HighResMIP, bias-corrected to ERA5-Land)." :
        "No climate model answered.", count ? "ok" : "error");
      renderAll();
    });
  }

  // =============================================================================================
  // FIELDS FOR THE CURRENT TIME
  // =============================================================================================

  function fieldAt(variable, index) {
    var key = variable + "|" + index;
    if (!state.fieldCache[key]) {
      var keys = Object.keys(state.fieldCache);
      if (keys.length > 40) {
        delete state.fieldCache[keys[0]];
      }
      state.fieldCache[key] = fieldFrom(state.grid.W, state.frames.values[variable][index], state.grid.n);
    }
    return state.fieldCache[key];
  }

  function windFieldsAt(index) {
    var key = "wind|" + index;
    if (!state.fieldCache[key]) {
      var speeds = state.frames.values.wind_speed_10m[index];
      var directions = state.frames.values.wind_direction_10m[index];
      var u = [], v = [];
      speeds.forEach(function (s, p) {
        var c = windComponents(s, directions[p]);
        u.push(c[0]);
        v.push(c[1]);
      });
      state.fieldCache[key] = { u: fieldFrom(state.grid.W, u, state.grid.n), v: fieldFrom(state.grid.W, v, state.grid.n) };
    }
    return state.fieldCache[key];
  }

  function stepHours() {
    return state.frames && state.frames.stepHours ? state.frames.stepHours : 1;
  }

  // Steps offered for playing: 15 minutes only when the frames are 15 minutes apart, and then
  // chosen by default (the teacher: "the basis is showing the last 15 minutes").
  function setStepOptions() {
    if (!ui.step) {
      return;
    }
    var options = [[60, "1 hour"], [180, "3 hours"], [360, "6 hours"], [1440, "1 day"]];
    if (stepHours() < 1) {
      options.unshift([15, "15 minutes"]);
    }
    // Default only when the kind of frames changes, so a 15-minute refresh keeps the user's step.
    // In History the default follows the length of the period (1 hour up to 3 days, 1 day beyond a month).
    var kind = state.mode === "history" ? "history-" + stepForDays(historyDays()) : String(stepHours());
    if (state.stepFor !== kind || !options.some(function (o) { return o[0] === state.stepMin; })) {
      state.stepMin = state.mode === "history" ? stepForDays(historyDays()) : options[0][0];
      state.stepFor = kind;
    }
    ui.step.textContent = "";
    options.forEach(function (o) {
      var option = el("option", { value: String(o[0]), text: "Step " + o[1] });
      option.selected = o[0] === state.stepMin;
      ui.step.appendChild(option);
    });
  }

  function hasFrames() {
    return state.mode !== "climate" && state.frames && state.grid;
  }

  // =============================================================================================
  // MAP DRAPE: an imagery layer drawn tile by tile from the field
  // =============================================================================================

  var drape = { layer: null, frameKey: null, pending: false };

  function makeProvider(kit, snapshot) {
    var bbox = snapshot.bbox;
    var events = kit.Event ? new kit.Event() : { addEventListener: function () { return function () {}; }, raiseEvent: function () {} };
    return {
      tilingScheme: kit.tilingScheme,
      rectangle: new kit.Rectangle(bbox.west * DEG, bbox.south * DEG, bbox.east * DEG, bbox.north * DEG),
      tileWidth: 256,
      tileHeight: 256,
      minimumLevel: 0,
      // The field is smooth (cells of about 250 m): beyond level 15 Cesium enlarges these tiles
      // instead of asking for new ones. Drawing street-level tiles made the map slow.
      maximumLevel: 15,
      tileDiscardPolicy: undefined,
      errorEvent: events,
      credit: undefined,
      proxy: undefined,
      hasAlphaChannel: true,
      ready: true,
      readyPromise: Promise.resolve(true),
      getTileCredits: function () { return undefined; },
      pickFeatures: function () { return undefined; },
      requestImage: function (x, y, level) {
        return Promise.resolve(renderTile(kit.tilingScheme, snapshot, x, y, level));
      }
    };
  }

  function renderTile(tilingScheme, snap, x, y, level) {
    var size = 256;
    var canvas = document.createElement("canvas");
    canvas.width = size;
    canvas.height = size;
    var ctx = canvas.getContext("2d");
    var rect = tilingScheme.tileXYToRectangle(x, y, level);
    var proj = tilingScheme.projection;
    var nw = proj.project({ longitude: rect.west, latitude: rect.north, height: 0 });
    var se = proj.project({ longitude: rect.east, latitude: rect.south, height: 0 });
    var westDeg = rect.west / DEG, eastDeg = rect.east / DEG;
    var rowLat = new Float64Array(size);
    for (var row = 0; row < size; row++) {
      var py = nw.y + (se.y - nw.y) * (row + 0.5) / size;
      rowLat[row] = proj.unproject({ x: nw.x, y: py, z: 0 }).latitude / DEG;
    }
    var lonAt = function (col) { return westDeg + (eastDeg - westDeg) * (col + 0.5) / size; };
    var pixelOf = function (lon, lat) {
      var p = proj.project({ longitude: lon * DEG, latitude: lat * DEG, height: 0 });
      return [(p.x - nw.x) / (se.x - nw.x) * size, (p.y - nw.y) / (se.y - nw.y) * size];
    };
    var b = snap.bbox, n = snap.n;

    if (snap.field) {
      var image = ctx.createImageData(size, size);
      var data = image.data;
      var edge = 0.06;
      for (var r = 0; r < size; r++) {
        var lat = rowLat[r];
        var fy = (lat - b.south) / (b.north - b.south);
        for (var c = 0; c < size; c++) {
          var lon = lonAt(c);
          var fx = (lon - b.west) / (b.east - b.west);
          if (fx < 0 || fx > 1 || fy < 0 || fy > 1) {
            continue;
          }
          var value = sampleField(snap.field, n, b, lon, lat);
          var colour = colourFor(snap.layer, value);
          if (!colour[3]) {
            continue;
          }
          // Soft edge, so the drawn area does not end in a hard line.
          var fade = Math.min(1, Math.min(fx, 1 - fx, fy, 1 - fy) / edge);
          var k = (r * size + c) * 4;
          data[k] = colour[0];
          data[k + 1] = colour[1];
          data[k + 2] = colour[2];
          data[k + 3] = Math.round(colour[3] * fade);
        }
      }
      ctx.putImageData(image, 0, 0);
    }

    // Outline of the averaging area (yellow, as in the mockups).
    if (snap.rings && snap.rings.length) {
      ctx.strokeStyle = "rgba(255, 216, 77, 0.95)";
      ctx.lineWidth = 2;
      snap.rings.forEach(function (ring) {
        ctx.beginPath();
        ring.forEach(function (coord, i) {
          var p = pixelOf(coord[0], coord[1]);
          if (i === 0) {
            ctx.moveTo(p[0], p[1]);
          } else {
            ctx.lineTo(p[0], p[1]);
          }
        });
        ctx.closePath();
        ctx.stroke();
      });
    }

    // Observed station values (ASTA), drawn over the model field.
    if (snap.stations) {
      ctx.font = "600 11px sans-serif";
      snap.stations.forEach(function (s) {
        var p = pixelOf(s.lon, s.lat);
        if (p[0] < -20 || p[0] > size + 20 || p[1] < -20 || p[1] > size + 20) {
          return;
        }
        var colour = colourFor(LAYERS.precipitation, s.value);
        ctx.beginPath();
        ctx.arc(p[0], p[1], 6, 0, 2 * Math.PI);
        ctx.fillStyle = colour[3] ? "rgb(" + colour.slice(0, 3).join(",") + ")" : "rgba(255,255,255,0.9)";
        ctx.fill();
        ctx.lineWidth = 2;
        ctx.strokeStyle = "#1b2130";
        ctx.stroke();
        if (level >= 11) {
          var label = s.name + " " + s.value.toFixed(1) + " mm";
          ctx.lineWidth = 3;
          ctx.strokeText(label, p[0] + 9, p[1] + 4);
          ctx.fillStyle = "#ffffff";
          ctx.fillText(label, p[0] + 9, p[1] + 4);
        }
      });
    }
    return canvas;
  }

  function currentSnapshot() {
    if (!state.grid) {
      return null;
    }
    var snap = { bbox: state.grid.bbox, n: state.grid.n, rings: state.area ? state.area.rings : [], layer: null, field: null, wind: null, stations: null };
    if (hasFrames() && state.layer !== "none") {
      snap.layer = LAYERS[state.layer];
      snap.field = fieldAt(snap.layer.variable, state.index);
    }
    if (state.mode === "now" && state.layer === "precipitation" && state.stations && state.stations.length) {
      snap.stations = state.stations;
    }
    return snap;
  }

  function refreshDrape() {
    if (drape.pending) {
      return;
    }
    drape.pending = true;
    requestAnimationFrame(function () {
      drape.pending = false;
      var s = scene();
      var kit = cesiumKit(s);
      if (!s || !kit) {
        setMapNote("Switch the map to 3D to see the weather on the terrain.");
        return;
      }
      setMapNote("");
      var snap = currentSnapshot();
      var old = drape.layer;
      drape.layer = null;
      if (snap) {
        drape.layer = s.imageryLayers.addImageryProvider(makeProvider(kit, snap));
        drape.layer.alpha = state.opacity;
      }
      if (old) {
        // Destroyed a few seconds later: destroying it at once freed textures that frames still
        // in flight used ("bindTexture: attempt to use a deleted object" in the console).
        s.imageryLayers.remove(old, false);
        setTimeout(function () {
          if (old.isDestroyed && !old.isDestroyed()) {
            old.destroy();
          }
        }, 3000);
      }
      repaint(s);
    });
  }

  // Terria rebuilds its imagery layers when the workbench or base map changes: keep ours on top.
  function guardDrape() {
    var s = scene();
    if (!s || !drape.layer) {
      return;
    }
    if (!s.imageryLayers.contains(drape.layer)) {
      drape.layer = null;
      refreshDrape();
    } else if (s.imageryLayers.indexOf(drape.layer) !== s.imageryLayers.length - 1) {
      s.imageryLayers.raiseToTop(drape.layer);
      repaint(s);
    }
  }

  // =============================================================================================
  // RAIN EFFECT (post-process stage)
  // =============================================================================================
  //
  // What the viewer sees follows the hourly rain of the time shown (the hour under the yellow line
  // in the chart), the wind of that hour and where the camera is:
  //   - more drops, larger and faster, as the rate rises: heavier rain has larger drops (the
  //     Marshall-Palmer size distribution), and larger drops fall faster (about 2-3 m/s for
  //     drizzle, up to about 9 m/s for large drops);
  //   - drops slanted by the wind blowing across the view (fall speed against wind speed);
  //   - three depth layers: near drops large and fast, far drops small and slow;
  //   - visibility drops with the rate: a grey veil, stronger towards the horizon in tilted views,
  //     and a wet, overcast look on the ground;
  //   - near the ground all layers; from the air only the far layers, but still visible, so a
  //     high bar in the chart always shows as falling rain.
  // It is a visual reading of the data, not a simulation of each drop.

  var RAIN_SHADER = [
    "uniform sampler2D colorTexture;",
    "uniform float density;",
    "uniform float size;",
    "uniform float speed;",
    "uniform float haze;",
    "uniform float nearGround;",
    "uniform float slant;",
    "uniform float horizon;",
    "uniform float time;",
    "in vec2 v_textureCoordinates;",
    "",
    "float hash12(vec2 p) {",
    "  vec3 p3 = fract(vec3(p.xyx) * 0.1031);",
    "  p3 += dot(p3, p3.yzx + 33.33);",
    "  return fract((p3.x + p3.y) * p3.z);",
    "}",
    "",
    "// One depth layer: cells = drops across a screen height, fall = screen heights per second,",
    "// len = streak length in cell heights, widthPx = streak width in pixels.",
    "float rainLayer(vec2 uv, float cells, float fall, float len, float widthPx, float seed) {",
    "  vec2 grid = vec2(cells, cells * 0.3);",
    "  vec2 p = uv * grid;",
    "  p.y += time * fall * grid.y;",
    "  vec2 cell = floor(p);",
    "  vec2 f = fract(p);",
    "  float present = step(hash12(cell + seed), density);",
    "  float x0 = 0.15 + 0.7 * hash12(cell + seed + 17.0);",
    "  float y0 = hash12(cell + seed + 31.0) * (1.0 - len);",
    "  float width = widthPx * cells / czm_viewport.w;",
    "  float across = 1.0 - smoothstep(0.0, width, abs(f.x - x0));",
    "  float t = (f.y - y0) / len;",
    "  float along = smoothstep(0.0, 0.08, t) * (1.0 - smoothstep(0.35, 1.0, t));",
    "  return present * across * along;",
    "}",
    "",
    "void main(void) {",
    "  vec4 base = texture(colorTexture, v_textureCoordinates);",
    "  vec2 uv = gl_FragCoord.xy / czm_viewport.w;",
    "  float c = cos(slant);",
    "  float s = sin(slant);",
    "  uv = mat2(c, s, -s, c) * uv;",
    "  float air = 0.8 + 0.2 * nearGround;",
    "  float streak = 0.0;",
    "  streak += 0.75 * nearGround * rainLayer(uv, 14.0, 1.30 * speed, 0.70, 3.0 * size, 1.0);",
    "  streak += 0.65 * air * rainLayer(uv + 0.37, 30.0, 0.95 * speed, 0.65, 2.0 * size, 2.0);",
    "  streak += 0.50 * air * rainLayer(uv + 0.71, 60.0, 0.70 * speed, 0.55, 1.5 * size, 3.0);",
    "  float grey = dot(base.rgb, vec3(0.299, 0.587, 0.114));",
    "  vec3 wet = mix(base.rgb, vec3(grey), 0.35) * 0.9;",
    "  vec3 colour = mix(base.rgb, wet, clamp(haze, 0.0, 1.0));",
    "  float far = mix(1.0, smoothstep(0.15, 1.0, v_textureCoordinates.y), horizon);",
    "  colour = mix(colour, vec3(0.62, 0.64, 0.66), haze * (0.15 + 0.6 * far));",
    "  colour = mix(colour, vec3(0.90, 0.93, 0.97), clamp(streak * 1.3, 0.0, 0.9));",
    "  out_FragColor = vec4(colour, base.a);",
    "}"
  ].join("\n");

  var rain = { stage: null, scene: null, look: null, slant: 0, looping: false, start: performance.now() };

  // Area-average wind of the time shown, as east and north components (m/s), or null.
  function windNow() {
    if (!hasFrames()) {
      return null;
    }
    var speeds = state.frames.values.wind_speed_10m[state.index];
    var directions = state.frames.values.wind_direction_10m[state.index];
    var us = [], vs = [];
    speeds.forEach(function (sp, k) {
      var comp = windComponents(sp, directions[k]);
      us.push(comp[0]);
      vs.push(comp[1]);
    });
    var u = weightedMean(state.grid.weights, us), v = weightedMean(state.grid.weights, vs);
    return u === null || v === null ? null : { u: u, v: v };
  }

  // Slant of the drops on screen: the wind blowing across the view against the fall speed.
  // (The sample grid is turned the other way, hence the minus sign.)
  function rainSlant(s, look) {
    var wind = windNow();
    if (!wind || !look) {
      return 0;
    }
    var heading = s.camera.heading;
    var across = wind.u * Math.cos(heading) - wind.v * Math.sin(heading);
    return -Math.max(-0.6, Math.min(0.6, Math.atan(across / look.fallMs)));
  }

  // 1 near the ground, 0 from about 600 m above it.
  function nearGroundFactor() {
    var s = scene();
    if (!s) {
      return 1;
    }
    var h = s.camera.positionCartographic.height;
    var ground = s.globe && s.globe.getHeight ? s.globe.getHeight(s.camera.positionCartographic) : undefined;
    var above = h - (ground === undefined ? 300 : ground);
    return Math.max(0, Math.min(1, 1 - (above - 30) / 600));
  }

  // 0 looking straight down, 1 looking at the horizon.
  function horizonFactor() {
    var s = scene();
    return s ? Math.max(0, Math.min(1, 1 + s.camera.pitch / (Math.PI / 2))) : 0;
  }

  function updateRain() {
    var s = scene();
    var kit = cesiumKit(s);
    if (!s || !kit || !kit.PostProcessStage) {
      return;
    }
    var mm = hasFrames() ? state.areaSeries.precipitation[state.index] : 0;
    var look = state.rainEffect ? rainLook(mm) : null;
    if (rain.scene !== s) {
      rain.stage = null;
      rain.scene = s;
    }
    if (look && !rain.stage) {
      var value = function (key) { return function () { return rain.look ? rain.look[key] : 0; }; };
      rain.stage = s.postProcessStages.add(new kit.PostProcessStage({
        name: "alzette_weather_rain",
        fragmentShader: RAIN_SHADER,
        uniforms: {
          density: value("density"),
          size: value("size"),
          speed: value("speed"),
          haze: value("haze"),
          nearGround: nearGroundFactor,
          slant: function () { return rain.slant; },
          horizon: horizonFactor,
          // Wrapped so the numbers stay small for the GPU.
          time: function () { return ((performance.now() - rain.start) / 1000) % 600; }
        }
      }));
    }
    // The stage stays enabled once created: the rain shows and hides through its uniforms
    // (density 0 and haze 0 leave the picture unchanged). Disabling a post-process stage and
    // enabling it again did not bring the rain back in this Terria build.
    rain.look = look;
    rain.slant = rainSlant(s, look);
    repaint(s);
    if (look && !rain.looping) {
      rain.looping = true;
      (function loop() {
        var sc = scene();
        if (!rain.stage || !rain.look || !sc) {
          rain.looping = false;
          // One more picture, so the last drops do not stay frozen on the map.
          repaint(sc);
          return;
        }
        // The camera turns while it rains: keep the slant across the current view.
        rain.slant = rainSlant(sc, rain.look);
        repaint(sc);
        requestAnimationFrame(loop);
      })();
    }
  }

  // =============================================================================================
  // VIEWPOINTS: stand on the bank at a gauge, at eye level
  // =============================================================================================

  // ---- the river: Luiz's water view, loaded through Terria's address like his own panel -------

  function riverShown() {
    return window.location.hash.indexOf("/terria-water-init/") >= 0;
  }

  // The day whose water replay is on the map ("2021-07-14"), or "" when the river is not a replay.
  function riverDayShown() {
    var match = /\/terria-water-init\/(?:.*\/)?date\/(\d{4}-\d{2}-\d{2})\.json/.exec(window.location.hash);
    return match ? match[1] : "";
  }

  // One day on the whole twin. In History, the map goes to the day chosen: Luiz's water replay of
  // that day is loaded, Terria's timeline then runs through it, and this panel follows that
  // timeline, so the rain, the water and the two clocks show the same moment. Before the gauge
  // records (December 2017), or on a day without stored readings, there is no water to replay:
  // a replay of another day is replaced by the river at its normal level, and the rain plays from
  // this panel. The camera is not moved.
  var HISTORY_CHOOSE = "Choose the days and press Show this period: the map (river and timeline) goes to these days too.";

  // The day under the yellow band (the period shown ends at the time of the index).
  function dayShown() {
    return localDateString(state.frames.times[state.index] - 1, CONFIG.timeZone);
  }

  function dayName(day) {
    return formatTime(Date.parse(day + "T12:00:00Z"), { weekday: "short", day: "numeric", month: "short", year: "numeric" });
  }

  function replayName(replay) {
    return replay.to > replay.from ? dayName(replay.from) + " to " + dayName(replay.to) : dayName(replay.from);
  }

  function historyNoteFor(replay, onMap) {
    var pick = replay.oneOfMany ? " Click another day in the chart to replay that one." : "";
    if (onMap) {
      return "The map replays " + replayName(replay) + ": the river and the map timeline run through it, and this panel follows." + pick;
    }
    return "The map timeline is on " + replayName(replay) + " and this panel follows it. The water of these days is being built: " +
      "it appears on the map in a moment (a minute or more the first time for new days)." + pick;
  }

  // The days the map replays for the History period shown.
  //  - Up to historyReplayMaxDays days: the whole period, from its first day with gauge readings
  //    to its last one. The map timeline then starts on the first day and ends on the last.
  //  - A longer period: one day, the one under the yellow band (the river data of many days is
  //    too heavy to build at once).
  // early: called as soon as the days are chosen, before the weather has arrived, so the river
  // starts loading at once; a long period needs the weather first (which day is shown).
  function mapToHistoryDay(early) {
    if (state.mode !== "history") {
      return Promise.resolve();
    }
    var whole = historyDays() <= CONFIG.historyReplayMaxDays;
    if (!whole && (early === true || !hasFrames())) {
      return Promise.resolve();
    }
    var token = state.token;
    var days = (whole ? daysOfPeriod(state.historyDate, historyEnd()) : [dayShown()])
      .filter(function (d) { return d >= CONFIG.waterHistoryStart; });
    return Promise.all(days.map(function (d) {
      return PROVIDERS.waterDay(d).then(function (water) { return water && water.times && water.times.length ? d : null; })
        .catch(function () { return null; });
    })).then(function (found) {
      if (token !== state.token || state.mode !== "history") {
        return;
      }
      var withWater = found.filter(Boolean);
      if (withWater.length) {
        var replay = { from: withWater[0], to: withWater[withWater.length - 1], oneOfMany: !whole };
        state.replay = replay;
        state.replayDay = replay.from;
        showRiver("day", replay.from, replay.to);
        stopPlaying();
        state.sync = true;
        ui.syncBox.checked = true;
        setHistoryNote(historyNoteFor(replay, false), true);
        return;
      }
      state.replay = null;
      state.replayDay = null;
      state.sync = false;
      ui.syncBox.checked = false;
      if (riverDayShown()) {
        showRiver("normal");
      }
      var what = whole ? periodText() : dayName(dayShown());
      setHistoryNote((whole ? historyEnd() : dayShown()) < CONFIG.waterHistoryStart ?
        "The gauge records start on " + CONFIG.waterHistoryStart + ": no river replay for " + what + ". Press Play for the rain." :
        "No gauge readings are stored for " + what + ": no river replay. Press Play for the rain.");
    });
  }

  // The time was moved by hand (slider released, chart clicked). In History with a replay on the
  // map: when the replay covers that time, the map timeline goes there and this panel keeps
  // following it (one clock); when it replays one day of a long period and another day was
  // chosen, the map goes to that day.
  function dayChosenByHand() {
    if (state.mode !== "history" || !hasFrames()) {
      return;
    }
    var replay = state.replay;
    var day = dayShown();
    if (replay && day >= replay.from && day <= replay.to && mapClockOnDay(replay.from, replay.to)) {
      // The start of the period under the yellow band.
      if (setMapClock(state.frames.times[state.index] - stepHours() * 3600000 + 1000)) {
        stopPlaying();
        state.sync = true;
        ui.syncBox.checked = true;
      }
      return;
    }
    if (!replay || replay.oneOfMany) {
      if (day !== state.replayDay) {
        mapToHistoryDay();
      }
    }
  }

  // Leaving History: a replay of a past day does not stay on the map under Now, Forecast or 2050.
  function mapLeavesHistory(nextMode) {
    setHistoryNote("");
    state.replayDay = null;
    state.replay = null;
    if (riverDayShown()) {
      state.sync = false;
      ui.syncBox.checked = false;
      showRiver(riverKindFor(nextMode));
    } else if (riverShown() && state.riverKind && state.riverKind !== riverKindFor(nextMode)) {
      // The river already on the map follows the mode: live in Now and Forecast, normal in 2050.
      showRiver(riverKindFor(nextMode));
    }
  }

  // The river level that goes with a mode: the latest gauge reading in Now and Forecast (there
  // is no river forecast, so Forecast keeps the latest level), the normal level in 2050.
  function riverKindFor(mode) {
    return mode === "now" || mode === "forecast" ? "live" : "normal";
  }

  // calm: plain information (grey) instead of a limit to notice (yellow).
  function setHistoryNote(text, calm) {
    state.historyNote = text;
    state.historyNoteCalm = !!calm;
    if (ui.historyNote) {
      ui.historyNote.textContent = text;
      ui.historyNote.style.display = text ? "" : "none";
      ui.historyNote.style.color = calm ? "#b8c4d0" : "";
    }
  }

  // The river at its normal level: the median stage of each gauge over 12 sample days of the
  // last full year (the 15th of each month), from the stored gauge readings. Independent of the
  // live readings and of the rain; computed once and kept in the browser.
  function normalLevels() {
    var year = new Date().getUTCFullYear() - 1;
    var key = "normal-levels:" + year;
    var cached = cacheGet(key);
    if (cached) {
      return Promise.resolve(cached);
    }
    var days = [];
    for (var m = 1; m <= 12; m++) {
      days.push(year + "-" + (m < 10 ? "0" : "") + m + "-15");
    }
    return Promise.all(days.map(function (d) {
      return PROVIDERS.waterDay(d).catch(function () { return null; });
    })).then(function (results) {
      var median = function (field) {
        var values = [];
        results.forEach(function (day) {
          (day ? day[field] : []).forEach(function (v) {
            if (v !== null && isFinite(v)) {
              values.push(v * 100);
            }
          });
        });
        values.sort(function (x, y) { return x - y; });
        return values.length ? percentile(values, 0.5) : null;
      };
      var levels = {
        year: year, days: results.filter(Boolean).length,
        pfaffenthal_cm: median("pfaffenthal"), hesperange_cm: median("hesperange"), livange_cm: median("livange")
      };
      if (levels.days >= 6) {
        cacheSet(key, levels);
      }
      return levels;
    });
  }

  function levelPath(levels) {
    var cm = function (v) { return v === null || v === undefined || !isFinite(v) ? "x" : Number(v).toFixed(1); };
    if (cm(levels.pfaffenthal_cm) === "x" && cm(levels.hesperange_cm) === "x") {
      return null;
    }
    return "level/" + cm(levels.pfaffenthal_cm) + "/" + cm(levels.hesperange_cm) + "/" + cm(levels.livange_cm);
  }

  // Options Luiz's panel puts in front of the water path (e.g. "buildings/" for his 3D buildings),
  // kept from the current address so loading the river does not switch them off.
  function riverPrefixes() {
    var match = window.location.hash.match(/\/terria-water-init\/(.*)$/);
    if (!match) {
      return "";
    }
    var segments = match[1].replace(/\.json$/, "").split("/");
    var prefixes = [];
    for (var i = 0; i < segments.length; i++) {
      if (segments[i] === "level" || segments[i] === "date") {
        break;
      }
      prefixes.push(segments[i]);
    }
    // Without a water view yet, Luiz's panel loads ".../buildings.json" or ".../names.json":
    // "buildings" is the option itself, a final "names" only means "names alone".
    if (i === segments.length && prefixes[prefixes.length - 1] === "names") {
      prefixes.pop();
    }
    return prefixes.length ? prefixes.join("/") + "/" : "";
  }

  // ---- putting a river view on the map ----------------------------------------------------------
  // A river view is one of Luiz's Terria view files (".../terria-water-init/...json"). Changing the
  // page address makes Terria load it, but Terria then applies again every view the address has
  // held since the page opened, each in the background, and the one whose data arrives last stays
  // on the map: a slow older view (12 November, still loading) replaced the day just chosen
  // (14 July 2021) while this panel showed July. So the view is given to Terria directly
  // (applyInitData, the call Terria itself uses for one view), one view at a time, and the address
  // is updated without waking Terria, so Luiz's panel, the flood cost popup and a page reload
  // still read the right view from it. Afterwards the day asked for is checked: when an older
  // load ends late and takes the map to another day, the day is put back.
  var river = { flight: 0, target: null, shown: "", best: null, loading: 0, clockSeen: null };

  function waterItemId() {
    var t = findTerria();
    var items = t && t.workbench && t.workbench.items ? t.workbench.items : [];
    for (var i = 0; i < items.length; i++) {
      if (typeof items[i].uniqueId === "string" && items[i].uniqueId.indexOf(CONFIG.waterItemPrefix) === 0) {
        return items[i].uniqueId;
      }
    }
    return null;
  }

  // The map timeline is on this day, or between this day and endDay.
  function mapClockOnDay(day, endDay) {
    var ms = terriaClockMs();
    if (ms === null) {
      return false;
    }
    var now = localDateString(ms, CONFIG.timeZone);
    return now >= day && now <= (endDay || day);
  }

  // path: the part of Luiz's address after the options ("date/2021-07-14", "level/70/58/40");
  // endDay: for a replay of several days, its last day (path is then the first day).
  function setRiverHash(path, endDay) {
    applyRiver(path, endDay || "", 0);
  }

  // Kept up while nothing is loading: when an older load still ended last and took the map to
  // another day, the replay asked for is put back.
  function pumpRiver() {
    var target = river.target;
    if (river.loading || !target || !target.day) {
      return;
    }
    if (target.tries < CONFIG.riverKeepTries && Date.now() - target.at > CONFIG.riverKeepEveryS * 1000 &&
        window.location.hash.slice(1) === target.url && !mapClockOnDay(target.day, target.endDay)) {
      applyRiver(target.path, target.endDay, target.tries + 1);
    }
  }

  // The full river view of the latest request is on the map (not only its timeline).
  function riverBuilt() {
    return !!river.best && river.best.flight === river.flight && river.best.full;
  }

  // A river view is given to Terria at once, even while an older one is still loading (it used to
  // wait behind it: the map then kept the previous days). A replay goes in two steps: first a
  // view with its timeline only, which loads at once, then the full view. When a load ends after
  // a newer one and takes the map, the newest view is put back.
  function applyRiver(path, endDay, tries) {
    var url = CONFIG.backendUrl + "/terria-water-init/" + riverPrefixes() + path + ".json";
    var day = /^date\/(\d{4}-\d{2}-\d{2})/.exec(path);
    var last = day && endDay && endDay > day[1] ? endDay : (day ? day[1] : "");
    var key = url + "|" + last;
    var t = findTerria();
    // Already this view on the map: nothing to do, unless it is a replay the map timeline is not
    // on (an older load took its place): then it is applied again. (The address must still be
    // ours: another panel that loaded its own view has changed it.)
    var same = window.location.hash.slice(1) === url && (river.shown === key || (!river.shown && (!day || last === day[1])));
    // (Still loading counts as on the map: it is asked for once.)
    if (same && !tries && (river.loading > 0 || !(day && !mapClockOnDay(day[1], last)))) {
      return;
    }
    if (!t || typeof t.applyInitData !== "function") {
      // Without that call: through the address, as before (one day only).
      river.target = null;
      river.shown = "";
      if (window.location.hash.slice(1) !== url) {
        window.location.hash = url;
      }
      return;
    }
    var flight = ++river.flight;
    river.target = { path: path, url: url, day: day ? day[1] : "", endDay: last, at: Date.now(), tries: tries };
    river.shown = key;
    river.clockSeen = null;
    try {
      // The address names the first day: a valid address of Luiz's, read by his panel, by the
      // flood cost popup and by a page reload.
      window.history.replaceState(window.history.state, "", "#" + url);
    } catch (e) {
      // The address stays as it was; the view is still applied below.
    }
    river.loading++;
    fetchJson(url, 60000).then(function (view) {
      if (flight !== river.flight) {
        return null;
      }
      if (!day) {
        return applyRiverStep(t, flight, true, view);
      }
      var days = daysInPeriod(day[1], last);
      var multiplier = CONFIG.riverReplayMultiplier * days;
      if (days > 1) {
        view = periodView(view, day[1], day[1], last, multiplier);
      }
      var label = "Alzette river " + (days > 1 ? day[1] + " to " + last : day[1]) + " (building the water…)";
      // Both steps start together: the timeline-only view is on the map within a moment, the
      // full one when the backend has built it.
      return Promise.all([
        applyRiverStep(t, flight, false, clockView(view, day[1], last, multiplier, label)),
        applyRiverStep(t, flight, true, view)
      ]);
    }).catch(function (error) {
      if (flight === river.flight) {
        river.target = null;
        river.shown = "";
        setMapNote("The river view could not be loaded: " + (error && error.message ? error.message : "unknown error") + ".");
      }
    }).then(function () {
      river.loading--;
    });
  }

  // One view to Terria. When it ends, the map must show the best view of the latest request (its
  // full view once that has loaded, else its timeline-only view): if an older or lesser load
  // ended last and took the map, that view is applied again.
  function applyRiverStep(t, flight, full, view) {
    return t.applyInitData({ initData: view }).then(function () {
      var best = river.best;
      var isBest = flight === river.flight && (full || !(best && best.flight === flight && best.full));
      if (isBest) {
        var wasTimelineOnly = best && best.flight === flight && !best.full;
        river.best = { flight: flight, full: full, view: view };
        if (river.target) {
          river.target.at = Date.now();
        }
        // The full view starts its timeline on the first day: keep the time the timeline-only
        // view had reached, so the replay goes on instead of jumping back.
        if (full && wasTimelineOnly && river.clockSeen !== null && river.target && river.target.day &&
            localDateString(river.clockSeen, CONFIG.timeZone) >= river.target.day &&
            localDateString(river.clockSeen, CONFIG.timeZone) <= river.target.endDay) {
          setMapClock(river.clockSeen);
        }
        if (full && state.mode === "history" && state.replay) {
          setHistoryNote(historyNoteFor(state.replay, true), true);
        }
        repaint();
        return;
      }
      if (best && best.flight === river.flight) {
        return applyRiverStep(t, best.flight, best.full, best.view);
      }
      return null;
    });
  }

  // Puts the map timeline on a time (real time in ms), as a click on Terria's own timeline would.
  // The gauge times are stored as local wall-clock times labelled UTC, so the map's clock is too.
  function setMapClock(ms) {
    var t = findTerria();
    try {
      var clock = t && t.timelineClock;
      var JulianDate = clock && clock.currentTime && clock.currentTime.constructor;
      if (!JulianDate || typeof JulianDate.fromDate !== "function") {
        return false;
      }
      var shown = CONFIG.waterTimesAreLocal ? ms + tzOffsetMinutes(ms, CONFIG.timeZone) * 60000 : ms;
      clock.currentTime = JulianDate.fromDate(new Date(shown));
      repaint();
      return true;
    } catch (e) {
      return false;
    }
  }

  // A flood scenario (HQ10, HQ100, extreme) unticked in Luiz's panel: his panel goes back to the
  // gauge level it had applied itself. When the river on the map came from this panel, or the
  // page has just opened, it has none to go back to and the flood stayed on the map. In that
  // case this panel puts the river back: the day replay in History, otherwise the normal level.
  function watchScenarioBoxes() {
    document.addEventListener("change", function (event) {
      var box = event.target;
      if (!box || box.checked || !/^water-level-control-hq/.test(box.id || "")) {
        return;
      }
      setTimeout(function () {
        var anyTicked = Array.prototype.some.call(document.querySelectorAll("input[id^='water-level-control-hq']"),
          function (other) { return other.checked; });
        // Still a scenario in the address: Luiz's panel did not go back by itself.
        if (anyTicked || !/\/terria-water-init\/.*level\/[^#]*hq/.test(window.location.hash)) {
          return;
        }
        if (state.mode === "history" && state.replayDay && hasFrames()) {
          mapToHistoryDay();
        } else {
          showRiver(riverKindFor(state.mode));
        }
      }, 700);
    }, true);
  }

  // Loads Luiz's 3D water surface, keeping his options (3D buildings and others) as they are.
  //   "normal" (default): the river at its normal level, flowing, whatever the rain and the gauges say now;
  //   "day": in History, the water of that day, which the map timeline then animates.
  // Resolves to true when the water was asked for.
  function showRiver(kind, day, endDay) {
    if (kind === "day") {
      state.riverKind = "day";
      day = day || state.historyDate;
      if (!(day && day >= CONFIG.waterHistoryStart)) {
        return Promise.resolve(false);
      }
      setRiverHash("date/" + day, endDay);
      setMapNote("");
      return Promise.resolve(true);
    }
    if (kind === "live") {
      setMapNote("Loading the river at the latest gauge reading…");
      return PROVIDERS.waterLatest().catch(function () { return null; }).then(function (latest) {
        if (latest) {
          state.waterLatest = latest;
        }
        var at = latest && latest.datetime ? Date.parse(String(latest.datetime).replace(/Z?$/, "Z")) : NaN;
        if (isFinite(at) && CONFIG.waterTimesAreLocal) {
          at = wallClockToUtc(String(latest.datetime).replace(/Z?$/, "Z"), CONFIG.timeZone);
        }
        var path = latest ? levelPath(latest) : null;
        if (!path || !isFinite(at) || Date.now() - at > CONFIG.liveMaxAgeH * 3600000) {
          // No recent reading: the normal level, said as such.
          return showRiver("normal").then(function (ok) {
            if (ok) {
              setMapNote("No gauge reading in the last " + CONFIG.liveMaxAgeH + " hours: the river is drawn at its normal level.");
            }
            return ok;
          });
        }
        state.riverKind = "live";
        setRiverHash(path);
        var he = latest.hesperange_cm;
        setMapNote("The Alzette at the latest gauge reading" + (he !== null && he !== undefined && isFinite(he) ?
          " (Hesperange " + fmt(he / 100, 2, "m") + ", " + formatTime(at, { hour: "2-digit", minute: "2-digit" }) + ")" : "") +
          (state.mode === "forecast" ? ". There is no river forecast: the river stays at this level." : "."));
        return true;
      });
    }
    setMapNote("Loading the river at its normal level…");
    state.riverKind = "normal";
    return normalLevels().then(function (levels) {
      var path = levelPath(levels);
      var note = "The Alzette at its normal level: median of the gauge readings on " + levels.days +
        " days of " + levels.year + " (the 15th of each month).";
      if (!path) {
        // No stored readings: the latest reading, said as such.
        path = levelPath(state.waterLatest || {});
        note = "No stored readings for a normal level: the river is drawn at the latest gauge reading.";
      }
      if (!path) {
        setMapNote("No gauge reading is available to draw the river with.");
        return false;
      }
      setRiverHash(path);
      setMapNote(note);
      return true;
    });
  }

  // After loading the river: fly to a view of the water at Hesperange, from about 400 m,
  // looking along the river; the whole area when the gauge position is not known.
  function flyToRiver() {
    // A viewpoint flight still under way is given up.
    standing.token++;
    var s = scene();
    var kit = cesiumKit(s);
    var g = (state.gauges || []).find(function (x) { return x.name === "Hesperange"; });
    if (!s || !kit) {
      return;
    }
    if (!g) {
      overview();
      return;
    }
    var flow = g.heading_deg * DEG;
    // Behind and above the gauge, looking downstream along the river.
    var back = 650;
    var lat = g.lat - back * Math.cos(flow) / EARTH_R / DEG;
    var lon = g.lon - back * Math.sin(flow) / (EARTH_R * Math.cos(g.lat * DEG)) / DEG;
    s.camera.flyTo({
      destination: kit.Cartesian3.fromDegrees(lon, lat, g.height_m + 420),
      orientation: { heading: flow, pitch: -32 * DEG, roll: 0 },
      duration: 2.5
    });
  }

  var EYE_HEIGHT_M = 1.7;
  // Where the viewer stands: on the bank, this far to the side of the river and downstream of
  // the gauge, looking at the water at the gauge.
  var BANK_SIDE_M = 40;
  var BANK_DOWNSTREAM_M = 15;

  // Bearing (radians, clockwise from north) and distance (m) from one point to another.
  function bearingTo(lon1, lat1, lon2, lat2) {
    var east = (lon2 - lon1) * DEG * Math.cos(lat1 * DEG) * EARTH_R;
    var north = (lat2 - lat1) * DEG * EARTH_R;
    return { heading: Math.atan2(east, north), distance: Math.sqrt(east * east + north * north) };
  }

  var standing = { token: 0 };

  // Calls done() as soon as ready() is true, checked ten times a second: not before minMs
  // (the map needs a moment to start loading), and at the latest after maxMs.
  function whenReady(ready, minMs, maxMs, done) {
    var started = Date.now();
    repaint();
    (function check() {
      var waited = Date.now() - started;
      var ok = false;
      try {
        ok = waited >= minMs && ready();
      } catch (e) {
        ok = false;
      }
      if (ok || waited >= maxMs) {
        done();
        return;
      }
      repaint();
      setTimeout(check, 100);
    })();
  }

  function standAt(name) {
    var s = scene();
    var kit = cesiumKit(s);
    if (!s || !kit) {
      setMapNote("Switch the map to 3D to use the viewpoints.");
      return;
    }
    var gaugesPromise = state.gauges ? Promise.resolve(state.gauges) : PROVIDERS.gauges().then(function (g) { state.gauges = g; return g; });
    gaugesPromise.then(function (gauges) {
      var g = gauges.find(function (item) { return item.name === name; });
      if (!g) {
        setMapNote("The position of " + name + " is not available.");
        return;
      }
      // The water surface at the gauge: gauge zero plus the level shown (or the latest reading).
      var level = gaugeLevels()[name];
      var water = g.height_m + (level ? level.level : 0);
      var flow = g.heading_deg * DEG;
      var side = flow + Math.PI / 2;
      var dNorth = BANK_SIDE_M * Math.cos(side) + BANK_DOWNSTREAM_M * Math.cos(flow);
      var dEast = BANK_SIDE_M * Math.sin(side) + BANK_DOWNSTREAM_M * Math.sin(flow);
      var lat = g.lat + dNorth / EARTH_R / DEG;
      var lon = g.lon + dEast / (EARTH_R * Math.cos(g.lat * DEG)) / DEG;
      var view = function () {
        var ground = s.globe.getHeight({ longitude: lon * DEG, latitude: lat * DEG, height: 0 });
        var eye = Math.max(ground === undefined ? water + 2 : ground, water) + EYE_HEIGHT_M;
        var aim = bearingTo(lon, lat, g.lon, g.lat);
        // Looking at the water at the gauge, never steeper than 30 degrees down.
        var pitch = Math.max(-30 * DEG, Math.min(-2 * DEG, Math.atan2(water - eye, Math.max(aim.distance, 1))));
        return {
          destination: kit.Cartesian3.fromDegrees(lon, lat, eye),
          orientation: { heading: aim.heading, pitch: pitch, roll: 0 }
        };
      };
      if (!riverShown()) {
        showRiver("normal");
      }
      setMapNote("Going to " + name + "…");
      // Two steps: first above the gauge, so the terrain there loads (a direct flight to eye level
      // from far away landed before the terrain was there), then down to the bank. Each step
      // goes on as soon as the terrain is loaded, instead of after a fixed wait: about 3 seconds
      // for a place already visited, and never longer than the fixed waits were.
      var token = ++standing.token;
      var mine = function () { return token === standing.token; };
      var groundReady = function () {
        return s.globe.tilesLoaded !== false &&
          s.globe.getHeight({ longitude: lon * DEG, latitude: lat * DEG, height: 0 }) !== undefined;
      };
      var above = view();
      var aim = above.orientation;
      s.camera.flyTo({
        destination: kit.Cartesian3.fromDegrees(lon, lat, water + 450),
        orientation: { heading: aim.heading, pitch: -45 * DEG, roll: 0 },
        duration: 1.2,
        complete: function () {
          whenReady(groundReady, 150, 1500, function () {
            if (!mine()) {
              return;
            }
            var eye = view();
            s.camera.flyTo({
              destination: eye.destination,
              orientation: eye.orientation,
              duration: 1.2,
              complete: function () {
                whenReady(groundReady, 100, 900, function () {
                  if (!mine()) {
                    return;
                  }
                  // The finest terrain tiles arrive last: correct the height once more.
                  var placed = view();
                  s.camera.setView(placed);
                  repaint(s);
                  positionCallouts();
                  // Luiz's water covers km 35-50 only: Livange (km 54.3) lies upstream of it.
                  setMapNote(g.river_km !== undefined && (g.river_km < CONFIG.waterReachKm[0] || g.river_km > CONFIG.waterReachKm[1]) ?
                    name + " (km " + g.river_km + ") is outside the modelled water reach, km " + CONFIG.waterReachKm.join("–") +
                    ": no 3D water surface is drawn here, only the gauge reading." : "");
                  // Finer terrain can still arrive a little later: adjust the eye height then,
                  // unless the camera has been moved in the meantime.
                  var at = s.camera.position, x = at.x, y = at.y, z = at.z;
                  var eyeHeight = s.camera.positionCartographic.height;
                  setTimeout(function () {
                    var now = s.camera.position;
                    if (!mine() || Math.abs(now.x - x) + Math.abs(now.y - y) + Math.abs(now.z - z) > 0.5) {
                      return;
                    }
                    var ground = s.globe.getHeight({ longitude: lon * DEG, latitude: lat * DEG, height: 0 });
                    var wanted = Math.max(ground === undefined ? water + 2 : ground, water) + EYE_HEIGHT_M;
                    if (Math.abs(wanted - eyeHeight) > 0.5) {
                      s.camera.setView(view());
                      repaint(s);
                      positionCallouts();
                    }
                  }, 2500);
                });
              }
            });
          });
        }
      });
    }).catch(function () {
      setMapNote("Gauge positions are not available from the backend.");
    });
  }

  function overview() {
    // A viewpoint flight still under way is given up.
    standing.token++;
    var s = scene();
    var kit = cesiumKit(s);
    if (!s || !kit || !state.area) {
      return;
    }
    var b = state.area.bbox;
    var lon = (b.west + b.east) / 2;
    var lat = b.south - (b.north - b.south) * 0.9;
    s.camera.flyTo({
      destination: kit.Cartesian3.fromDegrees(lon, lat, 7000),
      orientation: { heading: 0, pitch: -35 * DEG, roll: 0 },
      duration: 2.5
    });
  }

  // =============================================================================================
  // CHART
  // =============================================================================================

  function niceStep(range, targetTicks) {
    var raw = range / Math.max(targetTicks, 1);
    var power = Math.pow(10, Math.floor(Math.log10(raw)));
    var unit = raw / power;
    return (unit <= 1 ? 1 : unit <= 2 ? 2 : unit <= 5 ? 5 : 10) * power;
  }

  function prepareCanvas(canvas) {
    var ratio = window.devicePixelRatio || 1;
    var width = canvas.clientWidth || 560, height = canvas.clientHeight || 190;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    var ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.font = "11px sans-serif";
    return { ctx: ctx, width: width, height: height };
  }

  var CHART_COLOURS = {
    text: "rgba(232,236,242,0.85)", grid: "rgba(255,255,255,0.08)", bars: "#3da4f5",
    barsFuture: "rgba(61,164,245,0.45)", band: "rgba(61,164,245,0.18)", water: "#c792ff",
    now: "rgba(255,255,255,0.55)", cursor: "#ffd84d", models: "rgba(160,190,230,0.45)",
    mean: "#ffd84d", reanalysis: "#7fe0a8", baseline: "rgba(255,255,255,0.05)"
  };

  // What the last drawing of the chart used: the width of its bars, and its time axis (for clicks).
  var chartState = { barHours: 1, left: 0, width: 0, t0: 0, t1: 0 };

  function drawSeriesChart(canvas) {
    var c = prepareCanvas(canvas);
    var ctx = c.ctx;
    if (!hasFrames()) {
      return;
    }
    var times = state.frames.times;
    var i0 = state.window[0], i1 = state.window[1];
    var stepMs = stepHours() * 3600000;
    var t0 = times[i0] - stepMs, t1 = times[i1];
    var precip = state.areaSeries.precipitation;
    var left = 42, right = 46, top = 22, bottom = 22;
    var w = c.width - left - right, h = c.height - top - bottom;
    var x = function (ms) { return left + (ms - t0) / (t1 - t0) * w; };

    // Bars: one per step as long as each gets at least 1.5 pixels (then the band of the time shown
    // and the bars match step for step). Over a longer window the bars widen to 1, 3, 6, 12 or
    // 24 hours (288 bars of 15 minutes, or 2000 hourly bars of a three-month period, were under a
    // pixel wide and vanished). A wider bar is the mean of the rates inside it, in mm/h, so the
    // scale keeps its meaning. Wide bars start at local midnight.
    var bars = [];
    var barH = barHours((t1 - t0) / 3600000, stepHours(), w, 1.5);
    chartState.barHours = barH;
    if (barH > stepHours()) {
      var barMs = barH * 3600000;
      var offset = tzOffsetMinutes(t1, CONFIG.timeZone) * 60000;
      var buckets = {};
      for (var q = i0; q <= i1; q++) {
        var end = Math.ceil((times[q] + offset) / barMs) * barMs - offset;
        var slot = buckets[end] || (buckets[end] = { sum: 0, n: 0 });
        if (precip[q] !== null && isFinite(precip[q])) {
          slot.sum += precip[q];
          slot.n++;
        }
      }
      Object.keys(buckets).forEach(function (end) {
        var slot = buckets[end];
        bars.push({ end: Number(end), length: barMs, value: slot.n ? slot.sum / slot.n : null });
      });
    } else {
      for (var k0 = i0; k0 <= i1; k0++) {
        bars.push({ end: times[k0], length: stepMs, value: precip[k0] });
      }
    }
    var maxP = 1;
    bars.forEach(function (bar) {
      maxP = Math.max(maxP, bar.value || 0);
    });
    var band = state.mode === "forecast" && state.ensemble ? state.ensemble : null;
    if (band) {
      band.times.forEach(function (ms, k) {
        if (ms > t0 && ms <= t1 && band.band.p90[k] !== null) {
          maxP = Math.max(maxP, band.band.p90[k]);
        }
      });
    }
    var stepP = niceStep(maxP, 4);
    maxP = Math.ceil(maxP / stepP) * stepP;
    var yP = function (v) { return top + h - v / maxP * h; };

    // Grid and left axis (rain).
    ctx.strokeStyle = CHART_COLOURS.grid;
    ctx.fillStyle = CHART_COLOURS.text;
    ctx.textAlign = "right";
    for (var gv = 0; gv <= maxP + 1e-9; gv += stepP) {
      ctx.beginPath();
      ctx.moveTo(left, yP(gv));
      ctx.lineTo(left + w, yP(gv));
      ctx.stroke();
      ctx.fillText(String(Math.round(gv * 10) / 10), left - 5, yP(gv) + 4);
    }
    ctx.save();
    ctx.translate(11, top + h / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.textAlign = "center";
    ctx.fillText("Rain (mm/h)", 0, 0);
    ctx.restore();

    // Ensemble band (10th to 90th percentile).
    if (band) {
      ctx.fillStyle = CHART_COLOURS.band;
      ctx.beginPath();
      var started = false;
      band.times.forEach(function (ms, k) {
        if (ms > t0 && ms <= t1 && band.band.p90[k] !== null) {
          if (!started) {
            ctx.moveTo(x(ms), yP(band.band.p90[k]));
            started = true;
          } else {
            ctx.lineTo(x(ms), yP(band.band.p90[k]));
          }
        }
      });
      for (var k = band.times.length - 1; k >= 0; k--) {
        var ms = band.times[k];
        if (ms > t0 && ms <= t1 && band.band.p10[k] !== null) {
          ctx.lineTo(x(ms), yP(band.band.p10[k]));
        }
      }
      ctx.closePath();
      ctx.fill();
    }

    // Rain bars: each bar covers the period before its time.
    var now = Date.now();
    bars.forEach(function (bar) {
      var v = bar.value;
      if (v === null || v <= 0 || bar.end <= t0) {
        return;
      }
      var xa = x(Math.max(bar.end - bar.length, t0)), xb = x(Math.min(bar.end, t1));
      ctx.fillStyle = bar.end > now ? CHART_COLOURS.barsFuture : CHART_COLOURS.bars;
      // A 1-pixel gap only when bars are wide enough to keep it.
      ctx.fillRect(xa, yP(v), Math.max(xb - xa - (xb - xa > 4 ? 1 : 0), 1.5), yP(0) - yP(v));
    });

    // Water level (right axis), only where measured.
    if (state.water) {
      var levels = [];
      state.water.times.forEach(function (ms, idx) {
        var lv = state.water.hesperange[idx];
        if (ms >= t0 && ms <= t1 && lv !== null) {
          levels.push([ms, lv]);
        }
      });
      if (levels.length > 1) {
        var lo = Infinity, hi = -Infinity;
        levels.forEach(function (p) { lo = Math.min(lo, p[1]); hi = Math.max(hi, p[1]); });
        var pad = Math.max((hi - lo) * 0.15, 0.05);
        lo -= pad;
        hi += pad;
        var yW = function (val) { return top + h - (val - lo) / (hi - lo) * h; };
        ctx.strokeStyle = CHART_COLOURS.water;
        ctx.lineWidth = 2;
        ctx.beginPath();
        levels.forEach(function (p, idx) {
          if (idx === 0) {
            ctx.moveTo(x(p[0]), yW(p[1]));
          } else {
            ctx.lineTo(x(p[0]), yW(p[1]));
          }
        });
        ctx.stroke();
        ctx.lineWidth = 1;
        ctx.fillStyle = CHART_COLOURS.water;
        ctx.textAlign = "left";
        var stepW = niceStep(hi - lo, 4);
        for (var wv = Math.ceil(lo / stepW) * stepW; wv <= hi; wv += stepW) {
          ctx.fillText(wv.toFixed(2), left + w + 5, yW(wv) + 4);
        }
        ctx.save();
        ctx.translate(c.width - 6, top + h / 2);
        ctx.rotate(Math.PI / 2);
        ctx.textAlign = "center";
        ctx.fillText("Hesperange (m)", 0, 0);
        ctx.restore();
      }
    }

    // Time axis.
    ctx.fillStyle = CHART_COLOURS.text;
    ctx.textAlign = "center";
    var spanH = (t1 - t0) / 3600000;
    var tickH = spanH <= 30 ? 3 : spanH <= 80 ? 12 : 24;
    // Over many days: a label every few days, far enough apart to stay readable.
    var pxPerDay = w / (spanH / 24);
    if (tickH === 24 && pxPerDay < 52) {
      tickH = 24 * Math.ceil(52 / pxPerDay);
    }
    // Ticks on local hours (a tick every 24 hours falls on local midnight, not on 01:00 or 02:00).
    var tickOffset = tzOffsetMinutes(t1, CONFIG.timeZone) * 60000;
    var firstTick = Math.ceil((t0 + tickOffset) / (tickH * 3600000)) * tickH * 3600000 - tickOffset;
    var dayLabel = spanH > 8 * 24 ? { day: "numeric", month: "short" } : { weekday: "short", day: "numeric" };
    for (var tk = firstTick; tk <= t1; tk += tickH * 3600000) {
      var label = tickH < 24 ? formatTime(tk, { hour: "2-digit", minute: "2-digit" }) : formatTime(tk, dayLabel);
      if (tickH < 24 && formatTime(tk, { hour: "2-digit" }) === "00") {
        label = formatTime(tk, dayLabel);
      }
      ctx.fillText(label, x(tk), c.height - 6);
    }
    // Thin lines between the days of a period of several days.
    if (spanH > 30 && pxPerDay >= 14) {
      ctx.strokeStyle = "rgba(255,255,255,0.16)";
      ctx.setLineDash([2, 3]);
      var firstDay = Math.ceil((t0 + tickOffset) / 86400000) * 86400000 - tickOffset;
      for (var dk = firstDay; dk < t1; dk += 86400000) {
        if (dk > t0) {
          ctx.beginPath();
          ctx.moveTo(x(dk), top);
          ctx.lineTo(x(dk), top + h);
          ctx.stroke();
        }
      }
      ctx.setLineDash([]);
    }
    // For a click on the chart: where the time axis lies.
    chartState.left = left;
    chartState.width = w;
    chartState.t0 = t0;
    chartState.t1 = t1;

    // Now and the time shown.
    if (now > t0 && now < t1) {
      ctx.strokeStyle = CHART_COLOURS.now;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(x(now), top);
      ctx.lineTo(x(now), top + h);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = CHART_COLOURS.now;
      ctx.fillText("now", x(now), top - 6);
    }
    // The period shown on the map: the rain of the step ENDING at the time shown. Drawn as a band
    // over that period, with the line in its middle, so the rain on the map is the rain of the bar
    // under the band (a line at the end of the period sat just after its bar, and the rain seemed
    // to keep falling after the line had passed).
    var shownEnd = times[state.index], shownStart = shownEnd - stepMs;
    var bandLeft = x(Math.max(shownStart, t0)), bandRight = x(shownEnd);
    ctx.fillStyle = "rgba(255, 216, 77, 0.22)";
    ctx.fillRect(bandLeft, top, Math.max(bandRight - bandLeft, 3), h);
    ctx.strokeStyle = CHART_COLOURS.cursor;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo((bandLeft + bandRight) / 2, top);
    ctx.lineTo((bandLeft + bandRight) / 2, top + h);
    ctx.stroke();
    ctx.lineWidth = 1;
  }

  var CLIMATE_VARIABLES = {
    total: { label: "Annual precipitation", unit: "mm", decimals: 0 },
    heavy: { label: "Days with 20 mm or more", unit: "days", decimals: 1 },
    temp: { label: "Mean air temperature", unit: "°C", decimals: 1 }
  };

  function drawClimateChart(canvas) {
    var c = prepareCanvas(canvas);
    var ctx = c.ctx;
    if (!state.climate) {
      return;
    }
    var field = state.climateVariable;
    var years = [];
    for (var y = 1950; y <= 2049; y++) {
      years.push(y);
    }
    var models = state.climate.models;
    var series = Object.keys(models).map(function (m) {
      var raw = years.map(function (year) { return models[m][year] ? models[m][year][field] : null; });
      return rollingMean(years, raw, 10);
    });
    var mean = years.map(function (_, i) {
      var values = series.map(function (s) { return s[i]; }).filter(function (v) { return v !== null; });
      return values.length ? values.reduce(function (a, b) { return a + b; }, 0) / values.length : null;
    });
    var observed = state.reanalysis ? years.map(function (year) {
      return state.reanalysis[year] ? state.reanalysis[year][field] : null;
    }) : null;

    var all = [].concat.apply([], series).concat(observed || []).filter(function (v) { return v !== null; });
    if (!all.length) {
      return;
    }
    var lo = Math.min.apply(null, all), hi = Math.max.apply(null, all);
    var pad = (hi - lo) * 0.08 || 1;
    lo -= pad;
    hi += pad;
    var left = 46, right = 14, top = 22, bottom = 22;
    var w = c.width - left - right, h = c.height - top - bottom;
    var x = function (year) { return left + (year - 1950) / 99 * w; };
    var yv = function (v) { return top + h - (v - lo) / (hi - lo) * h; };

    // Comparison periods.
    [CONFIG.baseline, CONFIG.future].forEach(function (period) {
      ctx.fillStyle = CHART_COLOURS.baseline;
      ctx.fillRect(x(period[0]), top, x(period[1]) - x(period[0]), h);
      ctx.fillStyle = CHART_COLOURS.text;
      ctx.textAlign = "center";
      ctx.fillText(period[0] + "–" + period[1], (x(period[0]) + x(period[1])) / 2, top - 6);
    });

    var step = niceStep(hi - lo, 4);
    ctx.textAlign = "right";
    for (var gv = Math.ceil(lo / step) * step; gv <= hi; gv += step) {
      ctx.strokeStyle = CHART_COLOURS.grid;
      ctx.beginPath();
      ctx.moveTo(left, yv(gv));
      ctx.lineTo(left + w, yv(gv));
      ctx.stroke();
      ctx.fillStyle = CHART_COLOURS.text;
      ctx.fillText(String(Math.round(gv * 10) / 10), left - 5, yv(gv) + 4);
    }
    ctx.textAlign = "center";
    for (var decade = 1950; decade <= 2040; decade += 10) {
      ctx.fillText(String(decade), x(decade), c.height - 6);
    }

    function line(values, colour, width) {
      ctx.strokeStyle = colour;
      ctx.lineWidth = width;
      ctx.beginPath();
      var drawing = false;
      values.forEach(function (v, i) {
        if (v === null) {
          drawing = false;
          return;
        }
        if (!drawing) {
          ctx.moveTo(x(years[i]), yv(v));
          drawing = true;
        } else {
          ctx.lineTo(x(years[i]), yv(v));
        }
      });
      ctx.stroke();
      ctx.lineWidth = 1;
    }
    series.forEach(function (s) { line(s, CHART_COLOURS.models, 1); });
    if (observed) {
      ctx.fillStyle = CHART_COLOURS.reanalysis;
      observed.forEach(function (v, i) {
        if (v !== null) {
          ctx.fillRect(x(years[i]) - 1.5, yv(v) - 1.5, 3, 3);
        }
      });
    }
    line(mean, CHART_COLOURS.mean, 2.5);
  }

  // =============================================================================================
  // FORMATTING
  // =============================================================================================

  function formatTime(ms, options) {
    return new Intl.DateTimeFormat("en-GB", Object.assign({ timeZone: CONFIG.timeZone }, options)).format(new Date(ms));
  }

  function formatFull(ms) {
    return formatTime(ms, { weekday: "short", day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  function fmt(value, decimals, unit) {
    if (value === null || value === undefined || !isFinite(value)) {
      return "–";
    }
    return value.toFixed(decimals) + (unit ? " " + unit : "");
  }

  function signed(value, decimals, unit) {
    if (value === null || !isFinite(value)) {
      return "–";
    }
    return (value > 0 ? "+" : value < 0 ? "−" : "±") + Math.abs(value).toFixed(decimals) + (unit ? " " + unit : "");
  }

  function compass(deg) {
    if (deg === null || !isFinite(deg)) {
      return "";
    }
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(((deg % 360) + 360) % 360 / 45) % 8];
  }

  // =============================================================================================
  // CARDS
  // =============================================================================================

  function cardsForSeries() {
    var cards = [];
    var times = state.frames.times;
    var i = state.index;
    var t = times[i];
    var p = state.areaSeries.precipitation;
    var mean = function (variable) { return state.areaSeries[variable][i]; };
    var source = state.mode === "history" ? "ERA5 reanalysis, area average" : "Weather model, area average";

    cards.push({ title: "Rain over the area", value: fmt(p[i], 1, "mm/h"), big: true,
      note: source + (stepHours() < 1 ? ", last 15 minutes as a rate" : "") });

    if (state.mode === "now") {
      var hr = state.hourlyRain;
      var hourNow = hr.times[Math.max(indexAtOrBefore(hr.times, Date.now()), 0)];
      cards.push({ title: "Last 24 hours", value: fmt(windowSum(hr.times, hr.values, hourNow - 86400000, hourNow), 1, "mm"), note: source });
      cards.push({ title: "Last 7 days", value: fmt(windowSum(hr.times, hr.values, hourNow - 7 * 86400000, hourNow), 1, "mm"), note: source });
    } else if (state.mode === "forecast" && !forecastSpan().all) {
      // A period of the forecast: its total, from the start of its first hour to its last time.
      var chosen = forecastSpan();
      var from = times[state.window[0]] - stepHours() * 3600000, to = times[state.window[1]];
      var range = "Forecast, area average";
      if (state.ensemble) {
        var totals = ensembleTotals(state.ensemble.times, state.ensemble.members, from, to);
        range = "Likely range " + fmt(totals.p10, 0) + "–" + fmt(totals.p90, 0) + " mm (" + state.ensemble.band.count + " ensemble members)";
      }
      var count = daysInPeriod(chosen.from, chosen.to);
      cards.push({ title: count === 1 ? "Total of " + formatTime(to - 1, { weekday: "short", day: "numeric", month: "short" }) : "Total of these " + count + " days",
        value: fmt(windowSum(times, p, from, to), 1, "mm"), note: range });
    } else if (state.mode === "forecast") {
      [["Next 24 hours", 1], ["Next 7 days", 7], ["Next " + CONFIG.forecastDays + " days", CONFIG.forecastDays]].forEach(function (spec) {
        var start = times[state.window[0]], end = start + spec[1] * 86400000;
        var note = "Forecast, area average";
        if (state.ensemble) {
          var tot = ensembleTotals(state.ensemble.times, state.ensemble.members, start, end);
          note = "Likely range " + fmt(tot.p10, 0) + "–" + fmt(tot.p90, 0) + " mm (" + state.ensemble.band.count + " ensemble members)";
        }
        cards.push({ title: spec[0], value: fmt(windowSum(times, p, start, end), 1, "mm"), note: note });
      });
    } else {
      var total = windowSum(times, p, times[0] - 3600000, times[times.length - 1]);
      var peak = 0;
      p.forEach(function (v, k) { if (v !== null && v > (p[peak] || 0)) { peak = k; } });
      var several = historyDays() > 1;
      cards.push({ title: several ? "Total of the " + historyDays() + " days" : "Day total", value: fmt(total, 1, "mm"), note: source });
      if (several) {
        // The day under the yellow band, from local midnight to midnight.
        var dayNow = localDateString(t - 1, CONFIG.timeZone);
        var daySum = 0, dayAny = false;
        p.forEach(function (v, k) {
          if (v !== null && localDateString(times[k] - 1, CONFIG.timeZone) === dayNow) {
            daySum += v * stepHours();
            dayAny = true;
          }
        });
        cards.push({ title: "Total of " + formatTime(t - 1, { weekday: "short", day: "numeric", month: "short" }),
          value: fmt(dayAny ? daySum : null, 1, "mm"), note: "The day under the yellow band" });
      }
      cards.push({ title: "Wettest hour", value: fmt(p[peak], 1, "mm/h"),
        note: "Ending " + formatTime(times[peak], several ? { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" } : { hour: "2-digit", minute: "2-digit" }) });
    }

    var wind = mean("wind_speed_10m");
    // Direction from the mean wind components: averaging angles would turn 350° and 10° into 180°.
    var us = [], vs = [];
    state.frames.values.wind_speed_10m[i].forEach(function (speed, k) {
      var comp = windComponents(speed, state.frames.values.wind_direction_10m[i][k]);
      us.push(comp[0]);
      vs.push(comp[1]);
    });
    var mu = weightedMean(state.grid.weights, us), mv = weightedMean(state.grid.weights, vs);
    var dir = mu === null || mv === null ? null : (Math.atan2(-mu, -mv) / DEG + 360) % 360;
    cards.push({ title: "Air temperature", value: fmt(mean("temperature_2m"), 1, "°C"), note: "Humidity " + fmt(mean("relative_humidity_2m"), 0, "%") });
    cards.push({ title: "Wind", value: fmt(wind, 1, "m/s"), note: wind !== null ? "From the " + compass(dir) : "" });

    var water = gaugeLevels().Hesperange;
    if (water) {
      cards.push({
        title: "Alzette water level at Hesperange", value: fmt(water.level, 2, "m"), big: true,
        change: water.change,
        note: water.latest ? "Latest gauge reading" : water.change !== null ? signed(water.change, 2, "m") + " in 3 hours" : "Gauge reading"
      });
    } else if (state.mode === "history" && dayShown() < CONFIG.waterHistoryStart) {
      cards.push({ title: "Alzette water level at Hesperange", value: "–", note: "Stored gauge readings start on 14 Dec 2017", big: true });
    } else if (state.mode === "history" && historyDays() > CONFIG.historyWaterMaxDays) {
      cards.push({ title: "Alzette water level at Hesperange", value: "–", big: true,
        note: "The gauge line is shown for periods up to " + CONFIG.historyWaterMaxDays + " days" });
    }
    // Headline cards first, as in the mockups.
    return cards.filter(function (c) { return c.big; }).concat(cards.filter(function (c) { return !c.big; }));
  }

  // Level of one gauge at ms (nearest reading within 45 minutes) and its change over 3 hours.
  function waterAt(ms, key) {
    var w = state.water;
    var series = w && w[key || "hesperange"];
    if (!series || !w.times.length) {
      return null;
    }
    var at = function (target) {
      var best = null, bestDiff = Infinity;
      w.times.forEach(function (tt, k) {
        var diff = Math.abs(tt - target);
        if (diff < bestDiff && series[k] !== null) {
          best = series[k];
          bestDiff = diff;
        }
      });
      return bestDiff <= 45 * 60000 ? best : null;
    };
    var level = at(ms);
    if (level === null) {
      return null;
    }
    var before = at(ms - 3 * 3600000);
    return { level: level, change: before === null ? null : level - before };
  }

  // Level at each gauge for the time shown; the latest reading when the time has none.
  var GAUGE_KEYS = { Pfaffenthal: "pfaffenthal", Hesperange: "hesperange", Livange: "livange" };

  function gaugeLevels() {
    var t = hasFrames() ? state.frames.times[state.index] : Date.now();
    var levels = {};
    Object.keys(GAUGE_KEYS).forEach(function (name) {
      var key = GAUGE_KEYS[name];
      var level = state.mode === "climate" ? null : waterAt(t, key);
      if (!level && state.mode !== "history" && state.mode !== "climate" && state.waterLatest) {
        var cm = state.waterLatest[key + "_cm"];
        if (cm !== null && cm !== undefined) {
          level = { level: cm / 100, change: null, latest: true };
        }
      }
      levels[name] = level;
    });
    return levels;
  }

  function cardsForClimate() {
    var cards = [];
    var models = state.climate ? state.climate.models : {};
    var names = Object.keys(models);
    if (!names.length) {
      return cards;
    }
    Object.keys(CLIMATE_VARIABLES).forEach(function (field) {
      var spec = CLIMATE_VARIABLES[field];
      var changes = names.map(function (m) {
        var a = periodMean(models[m], field, CONFIG.baseline[0], CONFIG.baseline[1]);
        var b = periodMean(models[m], field, CONFIG.future[0], CONFIG.future[1]);
        if (a === null || b === null) {
          return null;
        }
        return field === "total" ? (b - a) / a * 100 : b - a;
      }).filter(function (v) { return v !== null; });
      if (!changes.length) {
        return;
      }
      var avg = changes.reduce(function (s, v) { return s + v; }, 0) / changes.length;
      var unit = field === "total" ? "%" : field === "temp" ? "°C" : "days a year";
      var decimals = field === "total" ? 0 : 1;
      cards.push({
        title: spec.label,
        value: signed(avg, decimals, unit),
        note: "Models range " + signed(Math.min.apply(null, changes), decimals, field === "heavy" ? "" : unit) + " to " +
          signed(Math.max.apply(null, changes), decimals, field === "heavy" ? "" : unit)
      });
    });
    if (state.reanalysis) {
      var era = periodMean(state.reanalysis, "total", CONFIG.baseline[0], CONFIG.baseline[1]);
      var modelBase = names.map(function (m) { return periodMean(models[m], "total", CONFIG.baseline[0], CONFIG.baseline[1]); })
        .filter(function (v) { return v !== null; });
      var modelMean = modelBase.reduce(function (s, v) { return s + v; }, 0) / modelBase.length;
      cards.push({
        title: "Check against the past",
        value: fmt(modelMean, 0, "mm") + " vs " + fmt(era, 0, "mm"),
        note: "Models vs reanalysis, yearly rain 1981–2010"
      });
    }
    return cards;
  }

  // =============================================================================================
  // USER INTERFACE
  // =============================================================================================

  var ui = {};
  // v2: the docked layout. Positions saved by the first layout do not apply to it.
  var LAYOUT_KEY = "wx-layout-v2";

  var MODE_BADGES = {
    history: { text: "History", colour: "#8a63c9" },
    now: { text: "Live", colour: "#2e9d5b" },
    forecast: { text: "Forecast", colour: "#3f6fb5" },
    climate: { text: "Projection", colour: "#b5793f" }
  };

  var CSS = [
    ".wx-panel{position:fixed;z-index:1000;box-sizing:border-box;max-width:calc(100vw - 16px);max-height:calc(100vh - 16px);",
    "overflow:auto;background:rgba(32,38,52,0.95);color:#e8ecf2;border-radius:8px;box-shadow:0 4px 18px rgba(0,0,0,.35);",
    "font-size:13px;line-height:1.4}",
    ".wx-head{display:flex;align-items:center;gap:8px;padding:7px 10px;cursor:move;user-select:none;",
    "border-bottom:1px solid rgba(255,255,255,.08);position:sticky;top:0;background:rgb(32,38,52);z-index:1}",
    ".wx-head strong{flex:1;font-size:13.5px}",
    ".wx-head button{border:0;background:none;color:inherit;opacity:.75;cursor:pointer;font-size:15px;padding:0 4px}",
    ".wx-body{padding:10px}",
    ".wx-panel.wx-collapsed .wx-body{display:none}",
    // Top bar: mode, status and time.
    ".wx-bar{display:flex;align-items:center;gap:10px;padding:6px 8px;overflow:visible}",
    ".wx-bar.wx-busy::after{content:'';position:absolute;left:8px;right:8px;bottom:0;height:2px;border-radius:2px;",
    "background:linear-gradient(90deg,transparent,#7fc4ff,transparent) no-repeat;background-size:40% 100%;",
    "animation:wx-busy 1.1s linear infinite}",
    "@keyframes wx-busy{from{background-position:-70% 0}to{background-position:170% 0}}",
    "@media (prefers-reduced-motion:reduce){.wx-bar.wx-busy::after{animation:none;background:#7fc4ff;opacity:.6}}",
    // A panel that steps aside for another module's box moves smoothly, and back.
    ".wx-panel{transition:transform .18s ease}",
    "@media (prefers-reduced-motion:reduce){.wx-panel{transition:none}}",
    ".wx-modes{display:flex;gap:2px;background:rgba(255,255,255,.06);border-radius:6px;padding:2px}",
    ".wx-modes button{border:0;background:none;color:inherit;border-radius:5px;padding:5px 12px;cursor:pointer;font:inherit}",
    ".wx-modes button[aria-pressed=true]{background:#3f6fb5;color:#fff;font-weight:600}",
    ".wx-badge{display:inline-flex;align-items:center;gap:5px;border-radius:10px;padding:2px 9px;font-size:12px;font-weight:700;color:#fff;white-space:nowrap}",
    ".wx-badge::before{content:'';width:7px;height:7px;border-radius:50%;background:#fff;opacity:.9}",
    ".wx-bar-time{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0}",
    ".wx-row{display:flex;gap:6px;align-items:center;margin:6px 0}",
    ".wx-row > *{min-width:0}",
    ".wx-from-to{flex:0 0 34px;font-size:12px;opacity:.8}",
    ".wx-lengths{display:flex;flex-wrap:wrap;gap:5px;margin:2px 0 7px 40px}",
    ".wx-chip{border:0;border-radius:10px;padding:2px 8px;font:inherit;font-size:11.5px;background:rgba(255,255,255,.12);color:inherit;cursor:pointer}",
    ".wx-chip:hover{background:rgba(255,255,255,.2)}",
    ".wx-chip:focus-visible{outline:2px solid #ffd84d;outline-offset:1px}",
    ".wx-btn.wx-wide{display:block;width:100%;margin:2px 0 4px}",
    ".wx-chart canvas{cursor:pointer}",
    ".wx-panel input[type=date],.wx-panel select{flex:1;background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.15);",
    "border-radius:4px;color:inherit;padding:4px 6px;font:inherit}",
    ".wx-panel select option{background:#202634}",
    ".wx-btn{border:0;border-radius:4px;padding:5px 9px;font:inherit;font-weight:600;background:#3f6fb5;color:#fff;cursor:pointer;white-space:nowrap}",
    ".wx-btn.wx-quiet{background:rgba(255,255,255,.12);font-weight:400}",
    ".wx-btn:focus-visible,.wx-modes button:focus-visible{outline:2px solid #ffd84d;outline-offset:1px}",
    ".wx-area{font-size:11.5px;opacity:.8;margin:8px 0 0}",
    ".wx-area.wx-degraded{color:#ffd166;opacity:1}",
    // Summary cards: two headline values, then the rest.
    ".wx-cards{display:grid;grid-template-columns:1fr 1fr;gap:6px}",
    ".wx-card{background:rgba(255,255,255,.05);border-radius:6px;padding:6px 8px}",
    ".wx-card.wx-big{grid-column:1 / -1;padding:8px 10px}",
    ".wx-card .wx-t{font-size:11.5px;opacity:.75}",
    ".wx-card .wx-v{font-size:18px;font-weight:700;margin:1px 0}",
    ".wx-card.wx-big .wx-v{font-size:28px;line-height:1.15}",
    ".wx-card .wx-n{font-size:11.5px;opacity:.75}",
    ".wx-up{color:#ff7b6b;opacity:1!important}",
    ".wx-down{color:#7fe0a8;opacity:1!important}",
    ".wx-section{margin-top:10px;padding-top:8px;border-top:1px solid rgba(255,255,255,.08)}",
    ".wx-section:first-child{margin-top:0;padding-top:0;border-top:0}",
    ".wx-label{font-size:12px;font-weight:600;opacity:.8;margin-bottom:4px}",
    ".wx-layers label{display:flex;align-items:center;gap:6px;padding:2px 0;cursor:pointer}",
    ".wx-layers input{accent-color:#3f6fb5;margin:0}",
    ".wx-legend{display:flex;flex-wrap:wrap;gap:2px 8px;font-size:11px;opacity:.85;margin:3px 0 4px 20px}",
    ".wx-swatch{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:3px;vertical-align:-1px}",
    ".wx-ramp{height:8px;border-radius:2px;margin:4px 0 0 20px}",
    ".wx-ramp-labels{display:flex;justify-content:space-between;font-size:11px;opacity:.8;margin:0 0 4px 20px}",
    ".wx-panel input[type=range]{width:100%;accent-color:#3f6fb5}",
    ".wx-time{font-weight:600;background:rgba(255,255,255,.06);border-radius:5px;padding:4px 8px;margin-bottom:4px}",
    ".wx-range-labels{display:flex;justify-content:space-between;font-size:11px;opacity:.7}",
    ".wx-status{font-size:11.5px;opacity:.8;margin-top:8px}",
    ".wx-status.wx-error{color:#ff8a8a;opacity:1}",
    ".wx-status.wx-warn{color:#ffd166;opacity:1}",
    ".wx-note{font-size:11.5px;color:#ffd166;margin-top:4px}",
    ".wx-credit{font-size:10.5px;opacity:.55;margin-top:8px}",
    ".wx-views{display:grid;grid-template-columns:1fr 1fr;gap:4px}",
    ".wx-views .wx-btn{padding:5px 4px;font-size:12px}",
    // Bottom dock: time navigation beside the chart.
    ".wx-dock .wx-body{display:flex;gap:14px;align-items:stretch}",
    ".wx-dock-time{flex:0 0 270px;min-width:0}",
    ".wx-dock-chart{flex:1;min-width:0;display:flex;flex-direction:column}",
    ".wx-chart-head{display:flex;gap:10px;align-items:center;flex-wrap:wrap;font-size:11.5px;margin-bottom:2px}",
    ".wx-chart-head strong{font-size:12.5px;margin-right:4px}",
    ".wx-chart-head select{flex:0 1 auto}",
    ".wx-chart-head .wx-key{opacity:.85}",
    ".wx-chart canvas{width:100%;height:170px;display:block}",
    // Gauge labels pinned to the 3D map.
    ".wx-callout{position:fixed;left:0;top:0;z-index:900;pointer-events:none;will-change:transform}",
    ".wx-wind{position:absolute;pointer-events:none}",
    // Casual mode of the twin (twin-mode.js sets body.twin-casual): one simple panel only, so the
    // weather panels and what they draw on the map are for the In-depth mode.
    "body.twin-casual .wx-panel,body.twin-casual .wx-callout,body.twin-casual canvas.wx-wind{display:none!important}",
    ".wx-wind-legend{margin:0 0 4px 22px;font-size:11.5px}",
    ".wx-wind-row{display:flex;align-items:center;gap:6px;margin:1px 0}",
    ".wx-wind-swatch{flex:0 0 22px;height:3px;border-radius:2px}",
    ".wx-wind-note{opacity:.7;margin-top:3px;line-height:1.3}",
    ".wx-callout-box{position:absolute;bottom:30px;left:0;transform:translateX(-50%);white-space:nowrap;",
    "background:rgba(24,30,44,.92);color:#e8ecf2;border:1px solid rgba(140,200,255,.45);border-radius:7px;",
    "padding:4px 9px 5px;box-shadow:0 2px 10px rgba(0,0,0,.35);font-size:12px;line-height:1.25}",
    ".wx-callout-box b{display:block;font-size:17px}",
    ".wx-callout-stem{position:absolute;left:-1px;bottom:4px;width:2px;height:26px;background:rgba(140,200,255,.8)}",
    ".wx-callout-dot{position:absolute;left:-5px;bottom:-1px;width:10px;height:10px;border-radius:50%;",
    "background:#4db8ee;border:2px solid #fff;box-sizing:border-box}",
    "@media (prefers-reduced-motion: reduce){.wx-panel{scroll-behavior:auto}}"
  ].join("");

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") {
        node.textContent = attrs[k];
      } else if (k === "class") {
        node.className = attrs[k];
      } else if (k.indexOf("on") === 0) {
        node.addEventListener(k.slice(2), attrs[k]);
      } else {
        node.setAttribute(k, attrs[k]);
      }
    });
    (children || []).forEach(function (child) {
      if (child) {
        node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
      }
    });
    return node;
  }

  // ---- layout ---------------------------------------------------------------------------------

  function loadLayout() {
    try {
      return JSON.parse(localStorage.getItem(LAYOUT_KEY)) || {};
    } catch (e) {
      return {};
    }
  }

  function saveLayout(key, panel) {
    var layout = loadLayout();
    layout[key] = { left: panel.offsetLeft, top: panel.offsetTop, collapsed: panel.classList.contains("wx-collapsed") };
    try {
      localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout));
    } catch (e) {
      // Layout is a convenience only.
    }
  }

  // The free map area: right of Terria's side panel, left of Luiz's panel at the right edge.
  function mapBounds() {
    var left = 0, right = window.innerWidth;
    var explore = Array.prototype.find.call(document.querySelectorAll("button"), function (b) {
      return /Explore data/i.test(b.textContent || "");
    });
    for (var node = explore; node && node !== document.body; node = node.parentElement) {
      var r = node.getBoundingClientRect();
      if (r.width >= 250 && r.width <= 560 && r.height >= window.innerHeight * 0.45) {
        left = r.right;
        break;
      }
    }
    Array.prototype.forEach.call(document.querySelectorAll(".wl-panel"), function (panel) {
      var r = panel.getBoundingClientRect();
      if (r.width && r.right > window.innerWidth - 80) {
        right = Math.min(right, r.left);
      }
    });
    if (right - left < 700) {
      left = 0;
      right = window.innerWidth;
    }
    return { left: left, right: right };
  }

  // Default places, like the mockups: modes and time on top, map layers on the left,
  // the summary on the right, time navigation and the chart docked at the bottom.
  // Height of Terria's timeline at the bottom of the map, when a time-enabled layer shows it.
  function timelineHeight() {
    var height = 0;
    Array.prototype.forEach.call(document.querySelectorAll("[class*='imeline']"), function (node) {
      if (node.closest && node.closest(".wx-panel")) {
        return;
      }
      var r = node.getBoundingClientRect();
      if (r.height > 0 && r.height < 200 && r.bottom > window.innerHeight - 60) {
        height = Math.max(height, window.innerHeight - r.top);
      }
    });
    return height;
  }

  function defaultPlaces() {
    var b = mapBounds();
    var width = b.right - b.left;
    var barWidth = Math.min(660, width - 16);
    var side = 252, summary = 286;
    return {
      bar: { left: b.left + (width - barWidth) / 2, top: 58, width: barWidth },
      layers: { left: b.left + 8, top: 108, width: side },
      summary: { left: b.right - summary - 8, top: 108, width: summary },
      dock: { left: b.left + 8, bottom: Math.max(46, timelineHeight() + 8), width: width - 16 }
    };
  }

  function place(panel, spec, saved) {
    panel.style.width = spec.width + "px";
    panel.style.left = Math.max(0, Math.min(saved && saved.left !== undefined ? saved.left : spec.left, window.innerWidth - 120)) + "px";
    if (saved && saved.top !== undefined) {
      panel.style.top = Math.max(0, Math.min(saved.top, window.innerHeight - 40)) + "px";
      panel.style.bottom = "";
    } else if (spec.bottom !== undefined) {
      panel.style.top = "";
      panel.style.bottom = spec.bottom + "px";
    } else {
      panel.style.top = spec.top + "px";
      panel.style.bottom = "";
    }
  }

  var panels = {};

  function arrange(reset) {
    if (reset) {
      try {
        localStorage.removeItem(LAYOUT_KEY);
      } catch (e) {
        // Nothing saved.
      }
    }
    var places = defaultPlaces();
    var saved = loadLayout();
    Object.keys(panels).forEach(function (key) {
      place(panels[key], places[key], reset ? null : saved[key]);
      if (reset) {
        panels[key].classList.remove("wx-collapsed");
      }
    });
    renderChart();
  }

  // ---- making room for the boxes of other modules ---------------------------------------------
  // Terria's feature information box (Devika's flood damage popup opens in it) and Devika's
  // "Popup comparison" box are drawn under the weather panels. A weather panel that would cover
  // one of them steps aside while the box is open and comes back when it closes. The saved
  // places of the panels are not changed.
  var YIELD_GAP = 8;
  var yielding = { dragging: false };
  // Directions tried, in order, before a panel is hidden for as long as the box is open.
  var YIELD_MOVES = { bar: ["left", "right"], layers: ["down", "right"], summary: ["down", "left"] };

  function foreignBoxes() {
    var boxes = [];
    var nodes;
    try {
      nodes = document.querySelectorAll(CONFIG.yieldTo.join(","));
    } catch (e) {
      return boxes;
    }
    Array.prototype.forEach.call(nodes, function (node) {
      if (node.closest && node.closest(".wx-panel")) {
        return;
      }
      var r = node.getBoundingClientRect();
      if (r.width > 20 && r.height > 20 && window.getComputedStyle(node).visibility !== "hidden") {
        boxes.push({ left: r.left, top: r.top, right: r.right, bottom: r.bottom });
      }
    });
    return boxes;
  }

  function overlaps(a, b, gap) {
    return a.left < b.right + gap && a.right > b.left - gap && a.top < b.bottom + gap && a.bottom > b.top - gap;
  }

  function moved(rect, dx, dy) {
    return { left: rect.left + dx, right: rect.right + dx, top: rect.top + dy, bottom: rect.bottom + dy };
  }

  // Where a panel stands without its step aside (its layout place, which a shift does not change).
  function restingRect(panel) {
    return { left: panel.offsetLeft, top: panel.offsetTop, right: panel.offsetLeft + panel.offsetWidth,
      bottom: panel.offsetTop + panel.offsetHeight };
  }

  // Where a panel stands with its step aside, once it has finished moving.
  function standingRect(panel) {
    var shift = panel.wxShift || { x: 0, y: 0 };
    return moved(restingRect(panel), shift.x, shift.y);
  }

  function applyYield(panel, shift) {
    var before = panel.wxShift || { x: 0, y: 0, hidden: false, maxWidth: "" };
    if (before.x === shift.x && before.y === shift.y && before.hidden === shift.hidden && before.maxWidth === shift.maxWidth) {
      return false;
    }
    panel.wxShift = shift;
    panel.style.transform = shift.x || shift.y ? "translate(" + shift.x + "px," + shift.y + "px)" : "";
    panel.style.visibility = shift.hidden ? "hidden" : "";
    panel.style.maxWidth = shift.maxWidth;
    return true;
  }

  function yieldPanels() {
    if (yielding.dragging) {
      return;
    }
    var boxes = foreignBoxes();
    var bounds = boxes.length ? mapBounds() : null;
    var placed = [];
    ["bar", "layers", "summary", "dock"].forEach(function (key) {
      var panel = panels[key];
      if (!panel) {
        return;
      }
      var shift = { x: 0, y: 0, hidden: false, maxWidth: "" };
      // The dock is measured at its full width, not at the width it was narrowed to.
      var rest = restingRect(panel);
      if (key === "dock" && panel.wxShift && panel.wxShift.maxWidth) {
        rest.right = rest.left + parseFloat(panel.style.width || 0);
      }
      var hit = boxes.filter(function (box) { return overlaps(rest, box, 0); });
      if (hit.length && key === "dock") {
        // The dock keeps its place and becomes narrower, up to the left edge of the box.
        var edge = Math.min.apply(null, hit.map(function (box) { return box.left; }));
        var width = edge - YIELD_GAP - rest.left;
        if (width >= 420) {
          shift.maxWidth = Math.round(width) + "px";
        } else {
          shift.hidden = true;
        }
      } else if (hit.length) {
        var free = function (rect) {
          return rect.left >= bounds.left && rect.right <= window.innerWidth && rect.top >= 0 && rect.bottom <= window.innerHeight &&
            !boxes.some(function (box) { return overlaps(rect, box, YIELD_GAP - 1); }) &&
            !placed.some(function (other) { return overlaps(rect, other, 0); });
        };
        var top = Math.min.apply(null, hit.map(function (box) { return box.top; }));
        var bottom = Math.max.apply(null, hit.map(function (box) { return box.bottom; }));
        var left = Math.min.apply(null, hit.map(function (box) { return box.left; }));
        var right = Math.max.apply(null, hit.map(function (box) { return box.right; }));
        var offsets = {
          down: [0, bottom + YIELD_GAP - rest.top], up: [0, top - YIELD_GAP - rest.bottom],
          left: [left - YIELD_GAP - rest.right, 0], right: [right + YIELD_GAP - rest.left, 0]
        };
        var found = (YIELD_MOVES[key] || []).some(function (direction) {
          var offset = offsets[direction];
          if (free(moved(rest, offset[0], offset[1]))) {
            shift.x = Math.round(offset[0]);
            shift.y = Math.round(offset[1]);
            return true;
          }
          return false;
        });
        shift.hidden = !found;
      }
      var changed = applyYield(panel, shift);
      if (!shift.hidden) {
        var now = moved(rest, shift.x, shift.y);
        if (shift.maxWidth) {
          now.right = now.left + parseFloat(shift.maxWidth);
        }
        placed.push(now);
      }
      if (changed && key === "dock") {
        renderChart();
      }
    });
  }

  // The side panels end above the time navigation instead of running under it: on a low screen
  // their content scrolls inside the panel.
  function fitHeights() {
    var dock = panels.dock;
    if (!dock || yielding.dragging) {
      return;
    }
    var d = standingRect(dock);
    var dockShown = dock.style.visibility !== "hidden" && d.bottom > d.top;
    ["layers", "summary"].forEach(function (key) {
      var panel = panels[key];
      if (!panel) {
        return;
      }
      var r = standingRect(panel);
      var limit = "";
      if (dockShown && r.top < d.top && r.left < d.right && r.right > d.left) {
        limit = Math.max(140, Math.round(d.top - r.top - 8)) + "px";
      }
      if (panel.style.maxHeight !== limit) {
        panel.style.maxHeight = limit;
      }
    });
  }

  function makePanel(key, title, options) {
    options = options || {};
    var saved = loadLayout()[key] || {};
    var panel = el("div", { class: "wx-panel" + (options.extraClass ? " " + options.extraClass : ""), role: "region", "aria-label": title });
    if (saved.collapsed) {
      panel.classList.add("wx-collapsed");
    }
    var body = el("div", { class: "wx-body" });
    if (options.header !== false) {
      var toggle = el("button", { title: "Collapse or expand", "aria-label": "Collapse or expand " + title, text: "–" });
      var head = el("div", { class: "wx-head" }, [el("strong", { text: title }), toggle]);
      panel.appendChild(head);
      toggle.addEventListener("click", function () {
        panel.classList.toggle("wx-collapsed");
        saveLayout(key, panel);
        if (key === "dock") {
          renderChart();
        }
      });
      head.addEventListener("pointerdown", function (event) {
        if (event.target === toggle) {
          return;
        }
        var startX = event.clientX - panel.offsetLeft, startY = event.clientY - panel.offsetTop;
        panel.style.top = panel.offsetTop + "px";
        panel.style.bottom = "";
        function move(e) {
          panel.style.left = Math.max(0, Math.min(e.clientX - startX, window.innerWidth - 80)) + "px";
          panel.style.top = Math.max(0, Math.min(e.clientY - startY, window.innerHeight - 40)) + "px";
        }
        function up() {
          document.removeEventListener("pointermove", move);
          document.removeEventListener("pointerup", up);
          yielding.dragging = false;
          saveLayout(key, panel);
        }
        yielding.dragging = true;
        document.addEventListener("pointermove", move);
        document.addEventListener("pointerup", up);
      });
    }
    panel.appendChild(body);
    document.body.appendChild(panel);
    panels[key] = panel;
    return { panel: panel, body: body };
  }

  // ---- building the panels -------------------------------------------------------------------

  function buildUi() {
    document.head.appendChild(el("style", { text: CSS }));

    // Top bar.
    var bar = makePanel("bar", "Weather time mode", { header: false, extraClass: "wx-bar" });
    ui.modes = {};
    var modeRow = el("div", { class: "wx-modes", role: "group", "aria-label": "Time mode" });
    Object.keys(MODES).forEach(function (mode) {
      var button = el("button", { type: "button", text: MODES[mode].label, "aria-pressed": "false",
        onclick: function () {
          if (mode === state.mode && mode !== "history") {
            return;
          }
          if (mode === "history") {
            // Entering History does not load a river replay yet: only the days chosen do.
            if (state.mode !== "history") {
              setHistoryNote(HISTORY_CHOOSE, true);
            }
            load(mode);
          } else {
            mapLeavesHistory(mode);
            load(mode);
          }
        } });
      ui.modes[mode] = button;
      modeRow.appendChild(button);
    });
    ui.badge = el("span", { class: "wx-badge" });
    ui.barTime = el("span", { class: "wx-bar-time" });
    bar.panel.appendChild(modeRow);
    bar.panel.appendChild(ui.badge);
    bar.panel.appendChild(ui.barTime);
    bar.panel.appendChild(el("button", { type: "button", class: "wx-btn wx-quiet", text: "Arrange panels",
      title: "Put the weather panels back in their places", onclick: function () { arrange(true); } }));
    bar.panel.removeChild(bar.body);

    // Left: what is on the map, and the viewpoints.
    var layers = makePanel("layers", "Weather on the map").body;
    var layerSection = el("div", { class: "wx-section wx-layers" });
    ui.layerInputs = {};
    Object.keys(LAYERS).concat(["none"]).forEach(function (key) {
      var input = el("input", { type: "radio", name: "wx-layer", value: key });
      input.checked = state.layer === key;
      input.addEventListener("change", function () {
        state.layer = key;
        renderLegend();
        refreshDrape();
      });
      ui.layerInputs[key] = input;
      layerSection.appendChild(el("label", {}, [input, key === "none" ? "No weather field" : LAYERS[key].label + " (" + LAYERS[key].unit + ")"]));
      if (key !== "none") {
        layerSection.appendChild(el("div", { "data-legend": key }));
      }
    });
    var toggles = el("div", { class: "wx-section wx-layers" });
    var windBox = el("input", { type: "checkbox" });
    windBox.checked = state.wind;
    windBox.addEventListener("change", function () { state.wind = windBox.checked; updateWindLines(); });
    ui.windLegend = el("div", { class: "wx-wind-legend" });
    var rainBox = el("input", { type: "checkbox" });
    rainBox.checked = state.rainEffect;
    rainBox.addEventListener("change", function () { state.rainEffect = rainBox.checked; updateRain(); });
    var calloutBox = el("input", { type: "checkbox" });
    calloutBox.checked = state.callouts;
    calloutBox.addEventListener("change", function () { state.callouts = calloutBox.checked; renderCallouts(); });
    var opacity = el("input", { type: "range", min: "0.2", max: "1", step: "0.05", value: String(state.opacity), "aria-label": "Opacity of the weather field" });
    opacity.addEventListener("input", function () {
      state.opacity = Number(opacity.value);
      if (drape.layer) {
        drape.layer.alpha = state.opacity;
        var s = scene();
        if (s) {
          repaint(s);
        }
      }
    });
    toggles.appendChild(el("label", {}, [windBox, "Wind streamlines (3D)"]));
    toggles.appendChild(ui.windLegend);
    toggles.appendChild(el("label", {}, [rainBox, "Falling rain"]));
    toggles.appendChild(el("label", {}, [calloutBox, "Water levels at the gauges"]));
    toggles.appendChild(el("div", { class: "wx-row" }, [el("span", { text: "Opacity" }), opacity]));
    ui.mapNote = el("div", { class: "wx-note" });
    toggles.appendChild(ui.mapNote);
    var views = el("div", { class: "wx-views" });
    ["Pfaffenthal", "Hesperange", "Livange"].forEach(function (name) {
      views.appendChild(el("button", { type: "button", class: "wx-btn wx-quiet", text: "Stand at " + name, onclick: function () { standAt(name); } }));
    });
    views.appendChild(el("button", { type: "button", class: "wx-btn wx-quiet", text: "Whole area", onclick: overview }));
    views.appendChild(el("button", { type: "button", class: "wx-btn", text: "Show the river", style: "grid-column:1 / -1",
      title: "Goes to the river (Luiz's 3D water surface): at the latest gauge reading in Now and Forecast, the replay of the days chosen in History, the normal level in 2050",
      onclick: function () {
        // A day replay chosen in History stays on the map: the button then only goes to the river.
        if (state.mode === "history" && (riverDayShown() || (river.target && river.target.day))) {
          flyToRiver();
          return;
        }
        showRiver(riverKindFor(state.mode)).then(function (ok) { if (ok) { flyToRiver(); } });
      } }));
    layers.appendChild(layerSection);
    layers.appendChild(toggles);
    layers.appendChild(el("div", { class: "wx-section" }, [el("div", { class: "wx-label", text: "Viewpoints" }), views]));

    // Right: the summary.
    var summary = makePanel("summary", "Weather and water").body;
    ui.cards = el("div", { class: "wx-cards" });
    ui.area = el("div", { class: "wx-area" });
    ui.stationsNote = el("div", { class: "wx-note" });
    ui.status = el("div", { class: "wx-status" });
    summary.appendChild(ui.cards);
    summary.appendChild(ui.area);
    summary.appendChild(ui.stationsNote);
    summary.appendChild(ui.status);
    summary.appendChild(el("div", { class: "wx-credit", text: "Weather data: Open-Meteo.com (CC BY 4.0), with ECMWF, DWD and Météo-France models, " +
      "Copernicus ERA5 / ERA5-Land and CMIP6 HighResMIP. Stations: ASTA. Water levels: AGE via EDDIE." }));

    // Bottom: time navigation and chart.
    var dock = makePanel("dock", "Time navigation", { extraClass: "wx-dock" });
    ui.chartPanel = dock.panel;
    ui.dockTitle = dock.panel.querySelector(".wx-head strong");
    ui.timeSection = el("div", { class: "wx-dock-time" });
    ui.modeControls = el("div");
    ui.time = el("div", { class: "wx-time" });
    ui.slider = el("input", { id: "wx-time-slider", type: "range", min: "0", max: "0", value: "0", "aria-label": "Time shown" });
    ui.slider.addEventListener("input", function () {
      state.sync = false;
      ui.syncBox.checked = false;
      setIndex(Number(ui.slider.value));
    });
    ui.slider.addEventListener("change", dayChosenByHand);
    ui.rangeLabels = el("div", { class: "wx-range-labels" });
    ui.play = el("button", { type: "button", class: "wx-btn", text: "Play", onclick: togglePlay });
    ui.step = el("select", { "aria-label": "Step" });
    ui.step.addEventListener("change", function () { state.stepMin = Number(ui.step.value); });
    setStepOptions();
    ui.syncBox = el("input", { type: "checkbox" });
    ui.syncBox.addEventListener("change", function () {
      state.sync = ui.syncBox.checked;
      // One clock at a time: following the map timeline stops the panel's own Play.
      if (state.sync) {
        stopPlaying();
      }
    });
    [ui.modeControls, ui.time, ui.slider, ui.rangeLabels, el("div", { class: "wx-row" }, [ui.play, ui.step]),
      el("label", { class: "wx-row" }, [ui.syncBox, "Follow the map timeline (water replay)"])].forEach(function (n) {
      ui.timeSection.appendChild(n);
    });
    var chartColumn = el("div", { class: "wx-dock-chart" });
    ui.chartHead = el("div", { class: "wx-chart-head" });
    ui.canvas = el("canvas", { role: "img", "aria-label": "Chart" });
    // A click on the chart goes to that time (and, in History, the map goes to that day).
    ui.canvas.addEventListener("click", function (event) {
      if (!hasFrames() || !chartState.width) {
        return;
      }
      var box = ui.canvas.getBoundingClientRect();
      var fraction = (event.clientX - box.left - chartState.left) / chartState.width;
      if (fraction < 0 || fraction > 1) {
        return;
      }
      var ms = chartState.t0 + fraction * (chartState.t1 - chartState.t0);
      // The period that contains the click ends at the first time at or after it.
      var index = indexAtOrBefore(state.frames.times, ms) + 1;
      stopPlaying();
      state.sync = false;
      ui.syncBox.checked = false;
      setIndex(index);
      dayChosenByHand();
    });
    chartColumn.appendChild(ui.chartHead);
    chartColumn.appendChild(el("div", { class: "wx-chart" }, [ui.canvas]));
    dock.body.appendChild(ui.timeSection);
    dock.body.appendChild(chartColumn);

    arrange(false);
    window.addEventListener("resize", renderChart);
  }

  function setMapNote(text) {
    if (ui.mapNote) {
      ui.mapNote.textContent = text;
    }
  }

  // ---- gauge labels on the 3D map ------------------------------------------------------------

  // =============================================================================================
  // WIND STREAMLINES in 3D
  // =============================================================================================
  // The lines a particle would follow through the wind of the time shown, at several heights
  // above the ground: 10 m from the gridded weather (so the lines bend where the wind turns across
  // the area), and higher up from the wind at the centre of the area (120 m and 180 m from the
  // forecast models, 100 m from the archive). Colour is the speed; dashes move along each line
  // at a pace that grows with the speed.
  //
  // They are drawn on a canvas laid over the 3D view: each point is a position in space (terrain
  // height plus the height of the line) projected through the camera, so the lines stand above
  // the ground and move with the view. Drawing them this way needs no Cesium class Terria does
  // not expose, and it keeps moving while Terria has paused its own drawing. The price: a line
  // behind a hill or a building is still drawn.
  //
  // What it shows is the regional wind of the weather model. It follows the height of the
  // terrain; it does not flow around buildings or along the valley.
  var windLines = { canvas: null, scene: null, key: "", lines: [], layers: [], running: false, frame: 0, heightsAt: 0 };

  // Mean 10 m wind over the area at an index, from the mean of the components (averaging
  // directions would turn 350 and 10 degrees into 180).
  function areaWind(index) {
    var us = [], vs = [];
    state.frames.values.wind_speed_10m[index].forEach(function (speed, k) {
      var c = windComponents(speed, state.frames.values.wind_direction_10m[index][k]);
      us.push(c[0]);
      vs.push(c[1]);
    });
    var u = weightedMean(state.grid.weights, us), v = weightedMean(state.grid.weights, vs);
    if (u === null || v === null) {
      return null;
    }
    return { u: u, v: v, speed: Math.sqrt(u * u + v * v), direction: (Math.atan2(-u, -v) / DEG + 360) % 360 };
  }

  // The heights drawn at an index: 10 m always, the others when the source has them for this hour.
  function windLayersAt(index) {
    var base = areaWind(index);
    if (!base) {
      return [];
    }
    var layers = [{ height: 10, speed: base.speed, direction: base.direction, base: base }];
    var aloft = state.aloft;
    if (aloft && aloft.heights.length) {
      var k = indexAtOrBefore(aloft.times, state.frames.times[index]);
      aloft.heights.forEach(function (h) {
        var speed = k >= 0 ? aloft.speed[h][k] : null, direction = k >= 0 ? aloft.direction[h][k] : null;
        if (speed !== null && direction !== null && isFinite(speed) && isFinite(direction)) {
          layers.push({ height: h, speed: speed, direction: direction, base: base });
        }
      });
    }
    return layers;
  }

  // The wind of a height at a place: the 10 m wind there, turned and scaled to the wind of that
  // height (so the gentle bends of the 10 m field are kept); a uniform wind when the 10 m wind
  // is too weak to give a direction.
  function windSampler(layer, fields) {
    var bbox = state.grid.bbox, n = state.grid.n;
    var uniform = windComponents(layer.speed, layer.direction);
    return function (lon, lat) {
      var u = sampleField(fields.u, n, bbox, lon, lat), v = sampleField(fields.v, n, bbox, lon, lat);
      if (!isFinite(u) || !isFinite(v)) {
        return null;
      }
      if (layer.height === 10) {
        return [u, v];
      }
      if (layer.base.speed < 0.5 || Math.sqrt(u * u + v * v) < 0.3) {
        return uniform;
      }
      return turnVector(u, v, layer.direction - layer.base.direction, layer.speed / layer.base.speed);
    };
  }

  // The twin's Casual mode (twin-mode.js): the weather layer is shown in the In-depth mode only.
  function casualMode() {
    return document.body.classList.contains("twin-casual");
  }

  function windOn() {
    return state.wind && hasFrames() && state.mode !== "climate" && !casualMode();
  }

  // Build the lines of the time shown (only when the time or the data changed).
  function updateWindLines() {
    renderWindLegend();
    if (!windOn()) {
      windLines.lines = [];
      windLines.key = "";
      return;
    }
    var key = state.token + "|" + state.index + "|" + (state.aloft ? state.aloft.heights.join(",") : "-");
    if (key !== windLines.key) {
      windLines.key = key;
      var fields = windFieldsAt(state.index);
      var bbox = state.grid.bbox;
      var diagonalM = distanceM(bbox.west, bbox.south, bbox.east, bbox.north);
      var lines = [];
      windLines.layers.forEach(function (layer, li) {
        if (layer.speed < 0.3) {
          return;
        }
        var sample = windSampler(layer, fields);
        var mean = windComponents(layer.speed, layer.direction);
        var count = CONFIG.windLinesPerHeight[Math.min(li, CONFIG.windLinesPerHeight.length - 1)];
        streamSeeds(bbox, mean[0], mean[1], count, li * 0.33).forEach(function (seed, si) {
          var points = traceStreamline(sample, bbox, seed.lon, seed.lat, diagonalM / 56, 90);
          if (points.length < 6) {
            return;
          }
          var speed = points.reduce(function (sum, pt) { return sum + pt.speed; }, 0) / points.length;
          lines.push({ height: layer.height, points: points, speed: speed, colour: windColour(speed),
            width: 2.4 + li * 0.5, phase: (si * 37 + li * 53) % 100, world: null });
        });
      });
      windLines.lines = lines;
      windLines.heightsAt = 0;
    }
    startWindLoop();
  }

  // Positions in space of the points of the lines: terrain height plus the height of the line.
  // Terrain tiles arrive little by little, so this is done again every few seconds.
  function placeWindLines(s, kit) {
    var fallback = 280;
    if (state.gauges && state.gauges.length) {
      fallback = state.gauges.reduce(function (sum, g) { return sum + (g.height_m || 0); }, 0) / state.gauges.length + 15;
    }
    windLines.lines.forEach(function (line) {
      line.world = line.points.map(function (pt) {
        var ground = s.globe.getHeight({ longitude: pt.lon * DEG, latitude: pt.lat * DEG, height: 0 });
        pt.ground = ground === undefined || ground === null || !isFinite(ground) ? (pt.ground || fallback) : ground;
        return kit.Cartesian3.fromDegrees(pt.lon, pt.lat, pt.ground + line.height);
      });
    });
    windLines.heightsAt = Date.now();
  }

  function windCanvas(s) {
    var host = s.canvas && s.canvas.parentElement;
    if (!host) {
      return null;
    }
    if (!windLines.canvas || windLines.scene !== s || !windLines.canvas.isConnected) {
      if (windLines.canvas && windLines.canvas.parentElement) {
        windLines.canvas.parentElement.removeChild(windLines.canvas);
      }
      // Placed right after the 3D view's own canvas, so everything Terria lays over the map
      // (timeline, buttons, information boxes) stays above the lines.
      windLines.canvas = el("canvas", { class: "wx-wind", "aria-hidden": "true" });
      host.appendChild(windLines.canvas);
      windLines.scene = s;
      windLines.heightsAt = 0;
    }
    var c = windLines.canvas;
    var w = s.canvas.clientWidth, h = s.canvas.clientHeight;
    var ratio = Math.min(window.devicePixelRatio || 1, 2);
    if (c.width !== Math.round(w * ratio) || c.height !== Math.round(h * ratio)) {
      c.width = Math.round(w * ratio);
      c.height = Math.round(h * ratio);
    }
    c.style.left = s.canvas.offsetLeft + "px";
    c.style.top = s.canvas.offsetTop + "px";
    c.style.width = w + "px";
    c.style.height = h + "px";
    return { ctx: c.getContext("2d"), width: w, height: h, ratio: ratio };
  }

  function startWindLoop() {
    if (windLines.running) {
      return;
    }
    windLines.running = true;
    var tick = function () {
      if (!drawWindLines()) {
        windLines.running = false;
        return;
      }
      window.requestAnimationFrame(tick);
    };
    window.requestAnimationFrame(tick);
  }

  // One picture of the lines. Returns false when there is nothing to animate any more.
  function drawWindLines() {
    var s = scene();
    var kit = cesiumKit(s);
    var on = windOn() && s && kit && windLines.lines.length;
    if (!on) {
      if (windLines.canvas) {
        windLines.canvas.style.display = "none";
      }
      return false;
    }
    // Every third frame (about 20 pictures a second) is enough for the dashes.
    windLines.frame++;
    if (windLines.frame % 3) {
      return true;
    }
    var view = windCanvas(s);
    if (!view) {
      return true;
    }
    windLines.canvas.style.display = "";
    if (Date.now() - windLines.heightsAt > 3000 || !windLines.lines[0].world) {
      placeWindLines(s, kit);
    }
    var ctx = view.ctx;
    ctx.setTransform(view.ratio, 0, 0, view.ratio, 0, 0);
    ctx.clearRect(0, 0, view.width, view.height);
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    var cam = s.camera.position, dir = s.camera.direction;
    var seconds = performance.now() / 1000;
    // The lines are projected again only when the camera, the screen or the lines changed; in
    // between only the dashes move (projecting every point 30 times a second slowed the twin).
    var viewKey = [cam.x, cam.y, cam.z, dir.x, dir.y, dir.z].map(function (v) { return v.toFixed(2); }).join(",") +
      "|" + view.width + "x" + view.height + "|" + windLines.key + "|" + windLines.heightsAt;
    var reproject = viewKey !== windLines.viewKey;
    windLines.viewKey = viewKey;
    windLines.lines.forEach(function (line) {
      if (!reproject && line.runs) {
        drawWindRuns(ctx, line, line.runs, seconds);
        return;
      }
      // The parts of the line in front of the camera, as runs of screen points.
      var runs = [], run = [];
      line.world.forEach(function (position) {
        var ahead = (position.x - cam.x) * dir.x + (position.y - cam.y) * dir.y + (position.z - cam.z) * dir.z > 0;
        var p = ahead ? s.cartesianToCanvasCoordinates(position) : null;
        if (p && isFinite(p.x) && isFinite(p.y) && p.x > -400 && p.y > -400 && p.x < view.width + 400 && p.y < view.height + 400) {
          run.push(p);
        } else if (run.length) {
          runs.push(run);
          run = [];
        }
      });
      if (run.length) {
        runs.push(run);
      }
      line.runs = runs;
      drawWindRuns(ctx, line, runs, seconds);
    });
    ctx.globalAlpha = 1;
    return true;
  }

  function drawWindRuns(ctx, line, runs, seconds) {
      runs.forEach(function (points) {
        if (points.length < 2) {
          return;
        }
        var path = function () {
          ctx.beginPath();
          ctx.moveTo(points[0].x, points[0].y);
          for (var i = 1; i < points.length; i++) {
            ctx.lineTo(points[i].x, points[i].y);
          }
        };
        // A dark edge so the line reads on bright ground, the line itself faint, then bright
        // dashes that move downstream.
        ctx.setLineDash([]);
        ctx.globalAlpha = 0.3;
        ctx.strokeStyle = "#0e1622";
        ctx.lineWidth = line.width + 2;
        path();
        ctx.stroke();
        ctx.globalAlpha = 0.6;
        ctx.strokeStyle = line.colour;
        ctx.lineWidth = line.width;
        path();
        ctx.stroke();
        ctx.globalAlpha = 1;
        ctx.setLineDash([16, 22]);
        ctx.lineDashOffset = -((seconds * (14 + line.speed * 9) + line.phase) % 38);
        path();
        ctx.stroke();
        ctx.setLineDash([]);
        // Arrowheads at the middle and at the end of the run.
        [Math.floor(points.length * 0.5), points.length - 1].forEach(function (i) {
          var a = points[Math.max(0, i - 2)], b = points[i];
          var length = Math.sqrt((b.x - a.x) * (b.x - a.x) + (b.y - a.y) * (b.y - a.y));
          if (i < 3 || length < 2) {
            return;
          }
          var ux = (b.x - a.x) / length, uy = (b.y - a.y) / length, size = 7 + line.width * 1.6;
          ctx.fillStyle = line.colour;
          ctx.beginPath();
          ctx.moveTo(b.x + ux * size, b.y + uy * size);
          ctx.lineTo(b.x - uy * size * 0.6, b.y + ux * size * 0.6);
          ctx.lineTo(b.x + uy * size * 0.6, b.y - ux * size * 0.6);
          ctx.closePath();
          ctx.fill();
        });
      });
  }

  // Under the tick box: one row per height with its speed and direction at the time shown.
  function renderWindLegend() {
    if (!ui.windLegend) {
      return;
    }
    ui.windLegend.textContent = "";
    windLines.layers = windOn() ? windLayersAt(state.index) : [];
    if (!windOn()) {
      ui.windLegend.style.display = "none";
      return;
    }
    ui.windLegend.style.display = "";
    if (!scene()) {
      ui.windLegend.appendChild(el("div", { class: "wx-wind-note", text: "Switch the map to 3D to see the streamlines." }));
    }
    windLines.layers.slice().reverse().forEach(function (layer) {
      var swatch = el("span", { class: "wx-wind-swatch" });
      swatch.style.background = windColour(layer.speed);
      ui.windLegend.appendChild(el("div", { class: "wx-wind-row" }, [swatch,
        layer.height + " m: " + (layer.speed < 0.3 ? "calm" : fmt(layer.speed, 1, "m/s") + " from the " + compass(layer.direction))]));
    });
    var notes = [];
    if (windLines.layers.length === 1) {
      notes.push("The source has no wind above 10 m for this time.");
    }
    notes.push("Regional wind of the weather model, above the terrain. It does not flow around buildings.");
    ui.windLegend.appendChild(el("div", { class: "wx-wind-note", text: notes.join(" ") }));
  }

  var callouts = { items: {}, scene: null };

  function renderCallouts() {
    var s = scene();
    if (!s || !state.gauges || !state.gauges.length) {
      return;
    }
    if (callouts.scene !== s && s.postRender) {
      s.postRender.addEventListener(positionCallouts);
      callouts.scene = s;
    }
    var levels = gaugeLevels();
    state.gauges.forEach(function (g) {
      var item = callouts.items[g.name];
      if (!item) {
        item = { name: el("span", { text: g.name }), value: el("b"), change: el("span") };
        item.node = el("div", { class: "wx-callout", "aria-hidden": "true" }, [
          el("div", { class: "wx-callout-box" }, [item.name, item.value, item.change]),
          el("div", { class: "wx-callout-stem" }),
          el("div", { class: "wx-callout-dot" })
        ]);
        document.body.appendChild(item.node);
        callouts.items[g.name] = item;
      }
      var level = levels[g.name];
      item.level = level;
      item.value.textContent = level ? level.level.toFixed(2) + " m" : "no reading";
      if (level && level.change !== null && level.change !== undefined) {
        item.change.textContent = (level.change >= 0 ? "▲ " : "▼ ") + signed(level.change, 2, "m") + " in 3 h";
        item.change.className = level.change > 0.02 ? "wx-up" : level.change < -0.02 ? "wx-down" : "";
      } else {
        item.change.textContent = level && level.latest ? "latest reading" : "";
        item.change.className = "";
      }
    });
    positionCallouts();
  }

  function positionCallouts() {
    var s = scene();
    var kit = cesiumKit(s);
    var names = Object.keys(callouts.items);
    if (!names.length) {
      return;
    }
    var show = s && kit && state.callouts && state.mode !== "climate" &&
      s.camera.positionCartographic.height < 60000;
    var rect = s && s.canvas ? s.canvas.getBoundingClientRect() : null;
    // Near the ground (under about 400 m), labels of gauges more than 3 km away are hidden:
    // they keep their size on screen and would look as if they were in front of the viewer.
    var lowCamera = show && nearGroundFactor() > 0.4;
    names.forEach(function (name) {
      var item = callouts.items[name];
      var g = (state.gauges || []).find(function (x) { return x.name === name; });
      var visible = false;
      if (show && g && rect) {
        var height = g.height_m + (item.level ? item.level.level : 0) + 2;
        var position = kit.Cartesian3.fromDegrees(g.lon, g.lat, height);
        // Hidden when the gauge is behind the camera.
        var cam = s.camera.position, dir = s.camera.direction;
        var ahead = (position.x - cam.x) * dir.x + (position.y - cam.y) * dir.y + (position.z - cam.z) * dir.z > 0;
        var dx = position.x - cam.x, dy = position.y - cam.y, dz = position.z - cam.z;
        var tooFar = lowCamera && Math.sqrt(dx * dx + dy * dy + dz * dz) > 3000;
        var p = ahead && !tooFar ? s.cartesianToCanvasCoordinates(position) : null;
        visible = Boolean(p) && p.x >= 0 && p.y >= 40 && p.x <= rect.width && p.y <= rect.height;
        if (visible) {
          item.node.style.transform = "translate(" + Math.round(rect.left + p.x) + "px," + Math.round(rect.top + p.y) + "px)";
        }
      }
      item.node.style.display = visible ? "" : "none";
    });
  }

  // ---- rendering -----------------------------------------------------------------------------

  function renderModeControls() {
    Object.keys(ui.modes).forEach(function (mode) {
      ui.modes[mode].setAttribute("aria-pressed", String(mode === state.mode));
    });
    var badge = MODE_BADGES[state.mode];
    ui.badge.textContent = badge.text;
    ui.badge.style.background = badge.colour;
    ui.modeControls.textContent = "";
    if (state.mode === "history") {
      var max = localDateString(Date.now() - CONFIG.historyLagDays * 86400000, CONFIG.timeZone);
      if (!state.historyDate) {
        state.historyDate = max;
      }
      if (!state.historyEnd || state.historyEnd < state.historyDate) {
        state.historyEnd = state.historyDate;
      }
      periodControls({
        id: "wx-history", from: state.historyDate, to: state.historyEnd, min: CONFIG.historyFirstDate, max: max,
        lengths: [["1 day", 1], ["3 days", 3], ["1 week", 7], ["1 month", 31]],
        title: "Shows the weather of these days; the map (river and timeline) goes to these days too",
        show: function (from, to) {
          var period = clampPeriod(from, to, CONFIG.historyFirstDate, max, CONFIG.historyMaxDays);
          state.historyDate = period.from;
          state.historyEnd = period.to;
          state.periodNote = period.shortened ? "A period can be " + CONFIG.historyMaxDays +
            " days at most: it now ends on " + period.to + ". For whole years, see 2050 (yearly values since 1950)." : "";
          var loading = load("history");
          // The river starts loading at once, next to the weather, not after it.
          mapToHistoryDay(true);
          loading.then(function () { mapToHistoryDay(); });
        }
      });
      if (state.periodNote) {
        ui.modeControls.appendChild(el("div", { class: "wx-note", text: state.periodNote }));
      }
      ui.historyNote = el("div", { class: "wx-note", text: state.historyNote || "" });
      ui.historyNote.style.display = state.historyNote ? "" : "none";
      ui.historyNote.style.color = state.historyNoteCalm ? "#b8c4d0" : "";
      ui.modeControls.appendChild(ui.historyNote);
    } else if (state.mode === "forecast") {
      // The forecast always holds today and the days ahead; a period shows a part of it.
      var span = forecastSpan();
      periodControls({
        id: "wx-forecast", from: span.from, to: span.to, min: span.first, max: span.last,
        lengths: [["1 day", 1], ["3 days", 3], ["1 week", 7], ["All " + CONFIG.forecastDays + " days", CONFIG.forecastDays]],
        title: "Shows these days of the forecast",
        show: function (from, to) {
          var period = clampPeriod(from, to, span.first, span.last, CONFIG.forecastDays);
          var all = period.from === span.first && period.to === span.last;
          state.forecastFrom = all ? null : period.from;
          state.forecastTo = all ? null : period.to;
          // The forecast is already here (or remembered): only the days shown change.
          load("forecast");
        }
      });
      ui.modeControls.appendChild(el("div", { class: "wx-note", text: "The river has no forecast here: the map keeps the river as it is." }))
        .style.color = "#b8c4d0";
    }
    ui.timeSection.style.display = state.mode === "climate" ? "none" : "";
    ui.dockTitle.textContent = state.mode === "climate" ? "Climate to 2049" : "Time navigation";
  }

  // "From" and "To" day fields (the same day twice is one day), shortcuts for the length of the
  // period, and the button that shows it. Used by History and by Forecast.
  // options: id, from, to, min, max (days), lengths ([label, days]), title, show(from, to).
  function periodControls(options) {
    var from = el("input", { id: options.id + "-from", type: "date", min: options.min, max: options.max, value: options.from, "aria-label": "First day of the period" });
    var to = el("input", { id: options.id + "-to", type: "date", min: options.min, max: options.max, value: options.to, "aria-label": "Last day of the period" });
    var latest = function (day) { return day > options.max ? options.max : day; };
    // Changing the first day moves the period and keeps its length: one day stays one day
    // (otherwise picking 14 July 2021 with today as last day would ask for years).
    var length = daysInPeriod(options.from, options.to);
    from.addEventListener("change", function () {
      if (from.value) {
        to.value = latest(addDays(from.value, length - 1));
      }
    });
    to.addEventListener("change", function () {
      if (from.value && to.value && to.value >= from.value) {
        length = daysInPeriod(from.value, to.value);
      }
    });
    var lengths = el("div", { class: "wx-lengths" });
    options.lengths.forEach(function (spec) {
      lengths.appendChild(el("button", { type: "button", class: "wx-chip", text: spec[0],
        title: "Sets the last day so that the period is " + spec[1] + (spec[1] === 1 ? " day" : " days") + " long from the first day",
        onclick: function () {
          if (from.value) {
            length = spec[1];
            to.value = latest(addDays(from.value, length - 1));
          }
        } }));
    });
    var go = el("button", { type: "button", class: "wx-btn wx-wide", text: "Show this period", title: options.title,
      onclick: function () {
        if (from.value) {
          options.show(from.value, to.value);
        }
      } });
    ui.modeControls.appendChild(el("div", { class: "wx-row" }, [el("label", { class: "wx-from-to", for: options.id + "-from", text: "From" }), from]));
    ui.modeControls.appendChild(el("div", { class: "wx-row" }, [el("label", { class: "wx-from-to", for: options.id + "-to", text: "To" }), to]));
    ui.modeControls.appendChild(lengths);
    ui.modeControls.appendChild(go);
  }

  // The days of the forecast: first and last day it holds, and the part of it shown (all of it
  // unless a period was chosen; a period chosen on an earlier day is brought back inside).
  function forecastSpan() {
    var first = localDateString(Date.now(), CONFIG.timeZone);
    var last = addDays(first, CONFIG.forecastDays - 1);
    var from = state.forecastFrom && state.forecastFrom >= first && state.forecastFrom <= last ? state.forecastFrom : first;
    var to = state.forecastTo && state.forecastTo >= from && state.forecastTo <= last ? state.forecastTo : last;
    if (!state.forecastFrom) {
      to = last;
    }
    return { first: first, last: last, from: from, to: to, all: from === first && to === last };
  }

  function renderAreaNote() {
    if (!state.area || !ui.area) {
      return;
    }
    var a = state.area;
    var text = "Area: reach " + a.reachId + ", averaged over a " + a.variant;
    if (a.degraded) {
      text += ", used in place of the " + (a.requested || "watershed") + " because " +
        lowerFirst(a.reason || "the real layer is not available yet");
    }
    if (state.grid && state.grid.maskCount === 0) {
      text += ". The area has no polygon, so every sample point weighs the same";
    }
    ui.area.textContent = text + ".";
    ui.area.className = "wx-area" + (a.degraded ? " wx-degraded" : "");
  }

  function lowerFirst(text) {
    // "The layer…" -> "the layer…", but "AOI module…" stays as it is.
    var first = /^[A-Z][a-z]/.test(text) ? text.charAt(0).toLowerCase() : text.charAt(0);
    return first + text.slice(1).replace(/\.$/, "");
  }

  function renderLegend() {
    Object.keys(LAYERS).forEach(function (key) {
      var holder = document.querySelector("[data-legend=" + key + "]");
      if (!holder) {
        return;
      }
      holder.textContent = "";
      if (key !== state.layer) {
        return;
      }
      var layer = LAYERS[key];
      if (layer.classes) {
        var legend = el("div", { class: "wx-legend" });
        layer.classes.forEach(function (cls, i) {
          var next = layer.classes[i + 1];
          var label = next ? cls[0] + "–" + next[0] : "> " + cls[0];
          var swatch = el("span", { class: "wx-swatch" });
          swatch.style.background = cls[1];
          legend.appendChild(el("span", {}, [swatch, label]));
        });
        holder.appendChild(legend);
      } else {
        var ramp = el("div", { class: "wx-ramp" });
        var lo = layer.ramp[0][0], hi = layer.ramp[layer.ramp.length - 1][0];
        ramp.style.background = "linear-gradient(to right," + layer.ramp.map(function (r) {
          return r[1] + " " + ((r[0] - lo) / (hi - lo) * 100).toFixed(1) + "%";
        }).join(",") + ")";
        holder.appendChild(ramp);
        holder.appendChild(el("div", { class: "wx-ramp-labels" }, [el("span", { text: lo + " " + layer.unit }), el("span", { text: hi + " " + layer.unit })]));
      }
    });
  }

  function renderCards() {
    ui.cards.textContent = "";
    var cards = state.mode === "climate" ? cardsForClimate() : hasFrames() ? cardsForSeries() : [];
    cards.forEach(function (card, i) {
      // In 2050 mode the first card (rain change) is the headline.
      var big = card.big || (state.mode === "climate" && i === 0);
      var note = el("div", { class: "wx-n", text: card.note || "" });
      if (card.change !== undefined && card.change !== null) {
        note.className += card.change > 0.02 ? " wx-up" : card.change < -0.02 ? " wx-down" : "";
      }
      ui.cards.appendChild(el("div", { class: "wx-card" + (big ? " wx-big" : "") }, [
        el("div", { class: "wx-t", text: card.title }),
        el("div", { class: "wx-v", text: card.value }),
        note
      ]));
    });
  }

  function renderTime() {
    if (state.mode === "climate") {
      var text = "Change from " + CONFIG.baseline.join("–") + " to " + CONFIG.future.join("–");
      ui.time.textContent = text;
      ui.barTime.textContent = text + ", mean of the climate models";
      return;
    }
    if (!hasFrames()) {
      ui.time.textContent = "";
      ui.barTime.textContent = "";
      return;
    }
    var ms = state.frames.times[state.index];
    var kind = state.mode === "history" ? "" : ms > Date.now() ? "forecast" :
      stepHours() < 1 ? "past 15 minutes, model" : "past hour, model";
    // The period shown, e.g. "Sat 7 Nov, 14:45–15:00": the rain on the map fell in that period.
    var startLabel = { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" };
    if (state.mode === "history") {
      startLabel.year = "numeric";
    }
    var period = formatTime(ms - stepHours() * 3600000, startLabel) +
      "–" + formatTime(ms, { hour: "2-digit", minute: "2-digit" });
    ui.time.textContent = period + (kind ? " (" + kind + ")" : "");
    ui.barTime.textContent = period + " (Luxembourg time" + (kind ? ", " + kind : "") + ")";
    ui.slider.min = String(state.window[0]);
    ui.slider.max = String(state.window[1]);
    ui.slider.value = String(state.index);
    var short = { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" };
    ui.rangeLabels.textContent = "";
    ui.rangeLabels.appendChild(el("span", { text: formatTime(state.frames.times[state.window[0]], short) }));
    ui.rangeLabels.appendChild(el("span", { text: formatTime(state.frames.times[state.window[1]], short) }));
  }

  function renderChart() {
    if (!ui.canvas || ui.chartPanel.classList.contains("wx-collapsed")) {
      return;
    }
    ui.chartHead.textContent = "";
    var key = function (colour, label) {
      var s = el("span", { class: "wx-swatch" });
      s.style.background = colour;
      return el("span", { class: "wx-key" }, [s, label]);
    };
    if (state.mode === "climate") {
      ui.chartHead.appendChild(el("strong", { text: "Climate models to 2049" }));
      var select = el("select", { "aria-label": "Variable" });
      Object.keys(CLIMATE_VARIABLES).forEach(function (k) {
        var option = el("option", { value: k, text: CLIMATE_VARIABLES[k].label + " (" + CLIMATE_VARIABLES[k].unit + ")" });
        option.selected = state.climateVariable === k;
        select.appendChild(option);
      });
      select.addEventListener("change", function () { state.climateVariable = select.value; renderChart(); });
      ui.chartHead.appendChild(select);
      ui.chartHead.appendChild(key(CHART_COLOURS.models, "Each model, 10-year mean"));
      ui.chartHead.appendChild(key(CHART_COLOURS.mean, "Mean of the models"));
      if (state.reanalysis) {
        ui.chartHead.appendChild(key(CHART_COLOURS.reanalysis, "Reanalysis, each year"));
      }
      ui.canvas.setAttribute("aria-label", CLIMATE_VARIABLES[state.climateVariable].label + " from 1950 to 2049");
      drawClimateChart(ui.canvas);
      return;
    }
    ui.chartHead.appendChild(el("strong", { text: "Rain and water level" }));
    var barsKey = key(CHART_COLOURS.bars, "Area rain");
    ui.chartHead.appendChild(barsKey);
    ui.chartHead.appendChild(key("rgba(255, 216, 77, 0.6)", "Period shown on the map"));
    if (state.mode !== "history") {
      ui.chartHead.appendChild(key(CHART_COLOURS.barsFuture, "Forecast"));
    }
    if (state.mode === "forecast" && state.ensemble) {
      ui.chartHead.appendChild(key(CHART_COLOURS.band, "Likely range (10–90 %)"));
    }
    if (state.water) {
      ui.chartHead.appendChild(key(CHART_COLOURS.water, "Water level at Hesperange"));
    }
    ui.canvas.setAttribute("aria-label", "Rain over the area and the Alzette water level");
    drawSeriesChart(ui.canvas);
    // Named after the bars actually drawn (they widen over a long period).
    var bh = chartState.barHours;
    barsKey.lastChild.textContent = bh < 1 ? "Area rain every 15 minutes (as mm/h)" : bh === 1 ? "Area rain each hour" :
      bh === 24 ? "Area rain, mean of each day (mm/h)" : "Area rain, mean of each " + bh + " hours (mm/h)";
  }

  function renderAll() {
    renderModeControls();
    renderAreaNote();
    renderTime();
    renderCards();
    renderLegend();
    renderChart();
    renderCallouts();
    ui.stationsNote.textContent = state.mode === "now" ? state.stationsNote : "";
    refreshDrape();
    updateRain();
    updateWindLines();
  }


  // =============================================================================================
  // TIME NAVIGATION
  // =============================================================================================

  function setIndex(index) {
    if (!hasFrames()) {
      return;
    }
    state.index = Math.max(state.window[0], Math.min(state.window[1], index));
    renderTime();
    renderCards();
    renderChart();
    renderCallouts();
    refreshDrape();
    updateRain();
    updateWindLines();
  }

  var playTimer = null;

  function togglePlay() {
    if (state.playing) {
      stopPlaying();
      return;
    }
    if (!hasFrames()) {
      return;
    }
    state.playing = true;
    state.sync = false;
    ui.syncBox.checked = false;
    ui.play.textContent = "Pause";
    playTimer = setInterval(function () {
      // Both the panel's Play and the map timeline moved the time: the yellow line went back and
      // forth. The map timeline wins when it is followed.
      if (state.sync) {
        stopPlaying();
        return;
      }
      var next = state.index + Math.max(1, Math.round(state.stepMin / (stepHours() * 60)));
      setIndex(next > state.window[1] ? state.window[0] : next);
    }, 900);
  }

  function stopPlaying() {
    state.playing = false;
    if (playTimer) {
      clearInterval(playTimer);
      playTimer = null;
    }
    if (ui.play) {
      ui.play.textContent = "Play";
    }
  }

  // Follow Terria's clock (Luiz's water replay), when asked to and when the time is covered.
  function followClock() {
    if (!state.sync || !hasFrames()) {
      return;
    }
    var ms = terriaClockMs();
    if (ms === null) {
      return;
    }
    var times = state.frames.times;
    // While the map timeline is on another day (the replay is still loading, or it shows today),
    // keep the time shown instead of jumping to the end of the day.
    var slack = (stepHours() + 1) * 3600000;
    if (ms < times[state.window[0]] - slack || ms > times[state.window[1]] + slack) {
      return;
    }
    if (river.target && river.target.day && !riverBuilt()) {
      river.clockSeen = terriaClockMs();
    }
    var index = indexAtOrBefore(times, ms + stepHours() * 3600000 - 1);
    // When the timeline loops back, its clock can land just before the first period of the day:
    // snap to the first (or last) period instead of keeping the end-of-day values.
    index = Math.max(state.window[0], Math.min(state.window[1], index));
    if (index !== state.index) {
      setIndex(index);
    }
  }

  // =============================================================================================
  // START
  // =============================================================================================

  function loadGauges() {
    return Promise.all([
      PROVIDERS.gauges().then(function (g) { state.gauges = g; }).catch(function () { return null; }),
      PROVIDERS.waterLatest().then(function (w) { state.waterLatest = w; }).catch(function () { return null; })
    ]).then(renderCallouts);
  }

  function start() {
    buildUi();
    renderLegend();
    loadGauges();
    load("now").then(function () {
      var s = scene();
      // A first view like the mockups: tilted over the area, not straight down.
      if (s && s.camera.pitch < -75 * DEG) {
        overview();
      }
      // With the live weather on screen, quietly fetch what the next clicks need.
      setTimeout(fetchAhead, 3000);
    });
    // Step aside for the boxes of the other modules (feature information, flood cost comparison),
    // and keep the side panels above the time navigation.
    setInterval(function () {
      yieldPanels();
      fitHeights();
    }, 400);
    // Terria's side panel and Luiz's panel appear after this script: place the panels again
    // once they are there, unless the user has moved them.
    setTimeout(function () { arrange(false); }, 2500);
    setInterval(function () {
      if (hasFrames() && !drape.layer) {
        refreshDrape();
        updateRain();
      }
      guardDrape();
      // The streamlines stop when there is no 3D view; they start again when it is back.
      if (windOn() && !windLines.running) {
        updateWindLines();
      }
      if (state.mode !== "climate" && state.area && workbenchReachId() !== state.area.reachId) {
        load(state.mode);
      }
    }, 2000);
    watchScenarioBoxes();
    // The map timeline is followed twice a second, so the yellow band moves with it and not in
    // jumps; the next river view is asked for as soon as the previous one is on the map.
    setInterval(function () {
      pumpRiver();
      followClock();
    }, 500);
    setInterval(function () {
      if (state.mode === "now" && !state.playing) {
        load("now");
      }
      loadGauges();
      // A live river follows the new reading (nothing is reloaded when the level is the same).
      if ((state.mode === "now" || state.mode === "forecast") && state.riverKind === "live" && riverShown() && !riverDayShown()) {
        showRiver("live");
      }
    }, CONFIG.nowRefreshMin * 60000);
  }

  // Wait for Terria and its 3D scene (the panel also works in 2D, without the drape and the rain).
  var attempts = 0;
  (function waitForTerria() {
    attempts++;
    if (findTerria() || attempts > 60) {
      start();
      return;
    }
    setTimeout(waitForTerria, 1000);
  })();
})(typeof window !== "undefined" ? window : globalThis);
