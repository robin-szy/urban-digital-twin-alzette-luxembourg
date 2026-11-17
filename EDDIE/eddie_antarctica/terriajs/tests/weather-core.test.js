// Tests of the pure functions in terriajs/weather-control.js. No browser, Terria or network needed:
//   node --test EDDIE/eddie_antarctica/terriajs/tests/weather-core.test.js
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const core = require(path.join(__dirname, "..", "weather-control.js"));

const BOX = { west: 6.10, south: 49.52, east: 6.18, north: 49.63 };
const close = (a, b, tol) => assert.ok(Math.abs(a - b) <= tol, `${a} is not within ${tol} of ${b}`);

test("lattice respects the maximum number of points and covers the corners", () => {
  const lat = core.lattice(core.expandBbox(BOX, 3), 2.5, 36);
  assert.ok(lat.points.length <= 36);
  assert.ok(lat.points.length >= 4);
  const lons = lat.points.map((p) => p.lon), lats = lat.points.map((p) => p.lat);
  close(Math.min(...lons), BOX.west - 3 / (111.32 * Math.cos(49.575 * Math.PI / 180)), 1e-9);
  close(Math.max(...lats), BOX.north + 3 / 111.32, 1e-9);
});

test("interpolation weights of every cell sum to 1", () => {
  const points = core.lattice(BOX, 3, 12).points;
  const n = 8;
  const W = core.idwMatrix(points, BOX, n, 2);
  for (let c = 0; c < n * n; c++) {
    let total = 0;
    for (let p = 0; p < points.length; p++) {
      total += W[c * points.length + p];
    }
    close(total, 1, 1e-5);
  }
});

test("a uniform value gives a uniform field and the same area average", () => {
  const points = core.lattice(BOX, 3, 12).points;
  const n = 8;
  const W = core.idwMatrix(points, BOX, n, 2);
  const values = points.map(() => 4.2);
  const field = core.fieldFrom(W, values, n);
  field.forEach((v) => close(v, 4.2, 1e-5));
  const ring = [[6.12, 49.55], [6.16, 49.55], [6.16, 49.60], [6.12, 49.60], [6.12, 49.55]];
  const mask = core.areaMask([ring], BOX, n);
  assert.ok(mask.count > 0);
  const weights = core.areaWeights(W, mask.mask, n, points.length);
  close(weights.reduce((a, b) => a + b, 0), 1, 1e-9);
  close(core.weightedMean(weights, values), 4.2, 1e-9);
});

test("the area average stays within the range of the sample values", () => {
  const points = core.lattice(BOX, 3, 12).points;
  const n = 16;
  const W = core.idwMatrix(points, BOX, n, 2);
  const values = points.map((_, i) => (i * 7) % 5);
  const ring = [[6.11, 49.53], [6.17, 49.53], [6.17, 49.62], [6.11, 49.62], [6.11, 49.53]];
  const weights = core.areaWeights(W, core.areaMask([ring], BOX, n).mask, n, points.length);
  const mean = core.weightedMean(weights, values);
  assert.ok(mean >= Math.min(...values) && mean <= Math.max(...values));
});

test("missing values are left out, never treated as zero", () => {
  const points = [{ lon: 6.12, lat: 49.55 }, { lon: 6.16, lat: 49.60 }];
  const n = 4;
  const W = core.idwMatrix(points, BOX, n, 2);
  const field = core.fieldFrom(W, [3, null], n);
  field.forEach((v) => close(v, 3, 1e-6));
  assert.equal(core.weightedMean([0.5, 0.5], [3, null]), 3);
  assert.equal(core.weightedMean([0.5, 0.5], [null, null]), null);
});

test("bilinear sampling returns NaN outside the field", () => {
  const field = new Float32Array(4).fill(1);
  assert.ok(Number.isNaN(core.sampleField(field, 2, BOX, 5.0, 49.6)));
  close(core.sampleField(field, 2, BOX, 6.14, 49.575), 1, 1e-9);
});

test("point in polygon", () => {
  const ring = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]];
  assert.equal(core.pointInRings(5, 5, [ring]), true);
  assert.equal(core.pointInRings(15, 5, [ring]), false);
});

test("percentiles and the ensemble band", () => {
  assert.equal(core.percentile([1, 2, 3, 4, 5], 0.5), 3);
  close(core.percentile([0, 10], 0.1), 1, 1e-12);
  const hourly = {
    time: [0, 3600],
    precipitation: [1, 2],
    precipitation_member01: [0, 4],
    precipitation_member02: [2, 0],
    precipitation_probability: undefined
  };
  const members = core.ensembleMembers(hourly, "precipitation");
  assert.equal(members.length, 3);
  const band = core.ensembleBand(members);
  assert.equal(band.p50[0], 1);
  assert.equal(band.p50[1], 2);
});

test("window sums use (t0, t1], the hour ending at each time", () => {
  const times = [1, 2, 3, 4].map((h) => h * 3600000);
  const values = [1, 2, null, 4];
  assert.equal(core.windowSum(times, values, 1 * 3600000, 4 * 3600000), 6);
  assert.equal(core.windowSum(times, [null, null, null, null], 0, 5 * 3600000), null);
});

test("yearly statistics skip incomplete years and count heavy days", () => {
  const days = [], values = [];
  for (let d = 0; d < 365; d++) {
    days.push(`2001-${String(1 + Math.floor(d / 31)).padStart(2, "0")}-01`);
    values.push(d === 10 ? 25 : 1);
  }
  days.push("2002-01-01");
  values.push(30);
  const stats = core.yearlyStats(days, values, 20);
  assert.equal(stats[2001].heavy, 1);
  close(stats[2001].total, 364 + 25, 1e-9);
  assert.equal(stats[2002], undefined);
});

test("local wall-clock times labelled UTC become real UTC (winter and summer)", () => {
  // 10:00 local in November is 09:00 UTC; in July it is 08:00 UTC.
  assert.equal(new Date(core.wallClockToUtc("2026-11-08T10:00:00Z", "Europe/Luxembourg")).toISOString(), "2026-11-08T09:00:00.000Z");
  assert.equal(new Date(core.wallClockToUtc("2026-07-08T10:00:00Z", "Europe/Luxembourg")).toISOString(), "2026-07-08T08:00:00.000Z");
  assert.equal(core.localDateString(Date.parse("2026-11-07T23:30:00Z"), "Europe/Luxembourg"), "2026-11-08");
});

test("colours: no rain is transparent, ramps are clamped", () => {
  const rain = core.LAYERS.precipitation;
  assert.equal(core.colourFor(rain, 0)[3], 0);
  assert.equal(core.colourFor(rain, 0.05)[3], 0);
  assert.ok(core.colourFor(rain, 3)[3] > 0);
  assert.ok(core.colourFor(rain, 0.2)[3] < core.colourFor(rain, 3)[3], "drizzle is more transparent than heavier rain");
  assert.ok(core.colourFor(rain, 0.2)[3] <= 80, "drizzle does not wash the map out");
  const temp = core.LAYERS.temperature;
  assert.deepEqual(core.colourFor(temp, -40), core.colourFor(temp, -10));
  assert.deepEqual(core.colourFor(temp, 60), core.colourFor(temp, 38));
  assert.equal(core.colourFor(temp, null)[3], 0);
});

test("wind from the west blows towards the east", () => {
  const [u, v] = core.windComponents(10, 270);
  close(u, 10, 1e-9);
  close(v, 0, 1e-9);
});

test("frames from several Open-Meteo locations", () => {
  const locations = [
    { hourly: { time: [0, 3600], precipitation: [1, 2] } },
    { hourly: { time: [0, 3600], precipitation: [3, null] } }
  ];
  const frames = core.framesFromLocations(locations, ["precipitation", "temperature_2m"]);
  assert.deepEqual(frames.times, [0, 3600000]);
  assert.deepEqual(frames.values.precipitation, [[1, 3], [2, null]]);
  assert.deepEqual(frames.values.temperature_2m, [[null, null], [null, null]]);
  assert.equal(core.asList({ a: 1 }).length, 1);
  assert.equal(core.indexAtOrBefore(frames.times, 1800000), 0);
  assert.equal(core.indexAtOrBefore(frames.times, -1), -1);
});

test("rain look: none below 0.1 mm/h, then denser, larger, faster and hazier as the rate rises", () => {
  assert.equal(core.rainLook(0), null);
  assert.equal(core.rainLook(0.05), null);
  assert.equal(core.rainLook(null), null);
  const rates = [0.1, 0.5, 2, 8, 25, 60];
  const looks = rates.map((r) => core.rainLook(r));
  for (let i = 1; i < looks.length; i++) {
    for (const key of ["density", "size", "speed", "haze", "fallMs"]) {
      assert.ok(looks[i][key] >= looks[i - 1][key], `${key} does not grow from ${rates[i - 1]} to ${rates[i]} mm/h`);
    }
  }
  assert.ok(looks[0].density > 0.15, "drizzle still shows some drops");
  assert.deepEqual(core.rainLook(25), core.rainLook(60), "25 mm/h and more share the strongest look");
  assert.ok(looks[0].fallMs >= 3 && looks[5].fallMs <= 9, "fall speeds stay between drizzle and large drops");
});

test("15-minute frames: rain as mm/h from 15-minute amounts, other variables from the nearest hour", () => {
  const hourly = { times: [0, 3600000], values: { precipitation: [[1, 1], [2, 2]], temperature_2m: [[10, 11], [12, 13]] } };
  const quarterTimes = [0, 900000, 1800000, 2700000, 3600000];
  const quarterRain = [[0.25, 0], [0.5, null], [0, 0], [1, 1], [0.1, 0.2]];
  const q = core.quarterHourFrames(hourly, quarterTimes, quarterRain);
  assert.equal(q.stepHours, 0.25);
  assert.deepEqual(q.times, quarterTimes);
  assert.deepEqual(q.values.precipitation[0], [1, 0]);
  assert.deepEqual(q.values.precipitation[1], [2, null]);
  close(q.values.precipitation[4][1], 0.8, 1e-12);
  assert.deepEqual(q.values.temperature_2m[1], [10, 11]);
  assert.deepEqual(q.values.temperature_2m[3], [12, 13]);
  // An hour of 15-minute rates, times a quarter of an hour each, gives back the amounts.
  const amount = [1, 2, 3].map((k) => q.values.precipitation[k][0] * 0.25).reduce((a, b) => a + b, 0);
  close(amount, 0.5 + 0 + 1, 1e-12);
});

test("periods of days: length, list, clamping to the archive and to the longest period", () => {
  assert.equal(core.addDays("2021-07-13", 3), "2021-07-16");
  assert.equal(core.addDays("2024-02-28", 2), "2024-03-01", "a leap year");
  assert.equal(core.addDays("2021-03-27", 2), "2021-03-29", "across the change to summer time");
  assert.equal(core.daysInPeriod("2021-07-13", "2021-07-16"), 4);
  assert.equal(core.daysInPeriod("2021-07-14", "2021-07-14"), 1);
  assert.deepEqual(core.daysOfPeriod("2021-07-30", "2021-08-02"), ["2021-07-30", "2021-07-31", "2021-08-01", "2021-08-02"]);

  // In order and inside the archive: unchanged.
  assert.deepEqual(core.clampPeriod("2021-07-13", "2021-07-16", "1950-01-01", "2026-11-13", 92),
    { from: "2021-07-13", to: "2021-07-16", days: 4, shortened: false });
  // No last day, or a last day before the first: one day.
  assert.equal(core.clampPeriod("2021-07-13", "", "1950-01-01", "2026-11-13", 92).days, 1);
  assert.equal(core.clampPeriod("2021-07-13", "2021-07-01", "1950-01-01", "2026-11-13", 92).to, "2021-07-13");
  // Too long: the end is brought closer, and said so.
  const long = core.clampPeriod("2021-01-01", "2021-12-31", "1950-01-01", "2026-11-13", 92);
  assert.equal(long.days, 92);
  assert.equal(long.to, "2021-04-02");
  assert.equal(long.shortened, true);
  // Beyond the last day of the archive: cut there.
  const late = core.clampPeriod("2026-11-10", "2026-11-20", "1950-01-01", "2026-11-13", 92);
  assert.deepEqual([late.from, late.to, late.days], ["2026-11-10", "2026-11-13", 4]);
  assert.equal(core.clampPeriod("1940-01-01", "1940-01-05", "1950-01-01", "2026-11-13", 92).from, "1950-01-01");
});

test("the step first offered grows with the period, and chart bars widen until they are visible", () => {
  assert.deepEqual([1, 3, 4, 10, 11, 31, 32, 92].map(core.stepForDays), [60, 60, 180, 180, 360, 360, 1440, 1440]);
  // One day of hourly data on 800 px: a bar per hour.
  assert.equal(core.barHours(24, 1, 800, 1.5), 1);
  // Four days: still hourly (8 px each); a month: 720 hourly bars would be 1.1 px, so 3-hour bars.
  assert.equal(core.barHours(96, 1, 800, 1.5), 1);
  assert.equal(core.barHours(31 * 24, 1, 800, 1.5), 3);
  // Three months: daily bars.
  assert.equal(core.barHours(92 * 24, 1, 800, 1.5), 6);
  assert.equal(core.barHours(92 * 24, 1, 300, 1.5), 12);
  assert.equal(core.barHours(92 * 24, 1, 120, 1.5), 24);
  // 15-minute data over two days on a narrow chart: hourly bars; never narrower than the step.
  assert.equal(core.barHours(54, 0.25, 300, 1.5), 1);
  assert.equal(core.barHours(6, 0.25, 800, 1.5), 0.25);
  assert.equal(core.barHours(24, 3, 800, 1.5), 3);
});

test("wind higher up: the 10 m wind turned clockwise and scaled", () => {
  // Blowing to the north (from 180); 90 degrees further round the compass it comes from the west, blowing east.
  const east = core.turnVector(0, 1, 90, 1);
  close(east[0], 1, 1e-12);
  close(east[1], 0, 1e-12);
  // The same turn as the difference of two wind directions, with the speed doubled.
  const from200 = core.windComponents(4, 200), from230 = core.windComponents(8, 230);
  const turned = core.turnVector(from200[0], from200[1], 230 - 200, 8 / 4);
  close(turned[0], from230[0], 1e-9);
  close(turned[1], from230[1], 1e-9);
});

test("streamlines: across the whole box, along the wind, bending where the wind turns", () => {
  const bbox = { west: 6.05, south: 49.5, east: 6.25, north: 49.66 };
  // A steady south-west wind (from 225): the line runs from the south-west edge to the north-east edge.
  const sw = core.windComponents(5, 225);
  const line = core.traceStreamline(() => sw, bbox, 6.15, 49.58, 300, 200);
  assert.ok(line.length > 20);
  const first = line[0], last = line[line.length - 1];
  assert.ok(first.lon < 6.15 && first.lat < 49.58, "starts upstream, to the south-west");
  assert.ok(last.lon > 6.15 && last.lat > 49.58, "ends downstream, to the north-east");
  for (let i = 1; i < line.length; i++) {
    assert.ok(line[i].lon > line[i - 1].lon && line[i].lat > line[i - 1].lat, "always downstream");
    close(core.distanceM(line[i - 1].lon, line[i - 1].lat, line[i].lon, line[i].lat), 300, 3);
    close(line[i].speed, 5, 1e-9);
  }
  // Both ends are within one step of an edge of the box.
  const toEdge = (p) => Math.min(core.distanceM(p.lon, p.lat, bbox.west, p.lat), core.distanceM(p.lon, p.lat, bbox.east, p.lat),
    core.distanceM(p.lon, p.lat, p.lon, bbox.south), core.distanceM(p.lon, p.lat, p.lon, bbox.north));
  assert.ok(toEdge(first) <= 300 && toEdge(last) <= 300);

  // A wind that turns from westerly in the west to southerly in the east: the line bends to the left.
  const turning = (lon) => core.windComponents(6, 270 - (lon - bbox.west) / (bbox.east - bbox.west) * 80);
  const bent = core.traceStreamline(turning, bbox, 6.1, 49.52, 300, 200);
  const heading = (a, b) => Math.atan2(b.lon - a.lon, b.lat - a.lat) * 180 / Math.PI;
  assert.ok(heading(bent[0], bent[1]) > heading(bent[bent.length - 2], bent[bent.length - 1]) + 20, "the heading turns towards north");

  // No wind, no data, or a start outside the box: no line.
  assert.deepEqual(core.traceStreamline(() => [0, 0], bbox, 6.15, 49.58, 300, 200), []);
  assert.deepEqual(core.traceStreamline(() => null, bbox, 6.15, 49.58, 300, 200), []);
  assert.deepEqual(core.traceStreamline(() => sw, bbox, 7, 49.58, 300, 200), []);
});

test("streamline starting points: evenly spread across the flow, inside the box", () => {
  const bbox = { west: 6.05, south: 49.5, east: 6.25, north: 49.66 };
  // West wind (blowing east): the points lie on a north-south line through the centre.
  const seeds = core.streamSeeds(bbox, 5, 0, 6, 0);
  assert.equal(seeds.length, 6);
  seeds.forEach((p) => {
    close(p.lon, 6.15, 1e-9);
    assert.ok(p.lat > bbox.south && p.lat < bbox.north);
  });
  const gaps = seeds.slice(1).map((p, i) => Math.abs(p.lat - seeds[i].lat));
  gaps.forEach((g) => close(g, gaps[0], 1e-9));
  // A shift moves them by part of a spacing; calm air gives none.
  const shifted = core.streamSeeds(bbox, 5, 0, 6, 0.33);
  close(Math.abs(shifted[0].lat - seeds[0].lat), gaps[0] * 0.33, 1e-9);
  assert.deepEqual(core.streamSeeds(bbox, 0, 0, 6, 0), []);
});

test("wind colours: light blue when slow, blue when moderate, purple when fast", () => {
  const rgb = (c) => c.startsWith("#") ? [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16)) : c.match(/\d+/g).map(Number);
  const slow = rgb(core.windColour(1)), moderate = rgb(core.windColour(6)), fast = rgb(core.windColour(12));
  assert.ok(slow[1] > slow[0] && slow[2] > 200, "slow is light blue");
  assert.deepEqual(moderate, [63, 125, 242]);
  assert.ok(fast[0] > 120 && fast[2] > 200 && fast[1] < 90, "fast is purple");
  assert.equal(core.windColour(40), core.windColour(16), "beyond the scale: the last colour");
  assert.ok(typeof core.windColour(null) === "string");
});

test("a one-day river view becomes a view of several days", () => {
  const view = {
    catalog: [
      { id: "alzette-3d-water-surface-ab12", type: "czml", name: "Alzette 3D water surface", url: "http://localhost:5000/water-surface.czml?date=2021-07-14" },
      { id: "alzette-3d-buildings-ab12", type: "czml", url: "http://localhost:5000/buildings-3d.czml?date=2021-07-14&without=livange" },
      { id: "alzette-place-names-streets", type: "czml", url: "http://localhost:5000/place-names/streets.czml" }
    ],
    workbench: ["alzette-3d-water-surface-ab12", "alzette-3d-buildings-ab12", "alzette-place-names-streets"],
    timeline: ["alzette-3d-water-surface-ab12"],
    settings: { depthTestAgainstTerrainEnabled: true }
  };
  const before = JSON.stringify(view);
  const period = core.periodView(view, "2021-07-14", "2021-07-14", "2021-07-16", 1800);
  assert.equal(JSON.stringify(view), before, "the view given is not changed");

  const water = period.catalog[0], buildings = period.catalog[1], names = period.catalog[2];
  // The date is replaced by the period and the speed; other arguments stay.
  assert.equal(water.url, "http://localhost:5000/water-surface.czml?start=2021-07-14T00%3A00%3A00Z&end=2021-07-16T23%3A59%3A59Z&multiplier=1800");
  assert.equal(buildings.url, "http://localhost:5000/buildings-3d.czml?start=2021-07-14T00%3A00%3A00Z&end=2021-07-16T23%3A59%3A59Z&multiplier=1800&without=livange");
  const args = new URL(water.url).searchParams;
  assert.equal(args.get("start"), "2021-07-14T00:00:00Z");
  assert.equal(args.get("end"), "2021-07-16T23:59:59Z");
  assert.equal(args.has("date"), false);
  // New ids, still starting like Luiz's (his map script finds the water layer by this start).
  assert.equal(water.id, "alzette-3d-water-surface-ab12-20210714-20210716");
  assert.ok(buildings.id.startsWith("alzette-3d-buildings-ab12-"));
  assert.deepEqual(period.workbench, [water.id, buildings.id, "alzette-place-names-streets"]);
  assert.deepEqual(period.timeline, [water.id]);
  // Layers without the date are untouched.
  assert.deepEqual(names, view.catalog[2]);
  assert.deepEqual(period.settings, view.settings);
  // A view of another day, or without layers: returned as it is.
  assert.deepEqual(core.periodView(view, "2021-07-15", "2021-07-15", "2021-07-16", 1200), view);
  assert.deepEqual(core.periodView({}, "2021-07-14", "2021-07-14", "2021-07-16", 1800), {});
});

test("a timeline-only river view: the clock over the days, without the slow layers", () => {
  const view = {
    catalog: [
      { id: "alzette-3d-water-surface-ab", type: "czml", url: "http://b/water-surface.czml?start=2021-07-14T00%3A00%3A00Z&end=2021-07-16T23%3A59%3A59Z" },
      { id: "alzette-3d-buildings-ab", type: "czml", url: "http://b/buildings-3d.czml?start=2021-07-14T00%3A00%3A00Z" },
      { id: "alzette-place-names-streets", type: "czml", url: "http://b/place-names/streets.czml" }
    ],
    workbench: ["alzette-3d-water-surface-ab", "alzette-3d-buildings-ab", "alzette-place-names-streets"],
    timeline: ["alzette-3d-water-surface-ab"],
    settings: { depthTestAgainstTerrainEnabled: true }
  };
  const before = JSON.stringify(view);
  const quick = core.clockView(view, "2021-07-14", "2021-07-16", 1800, "Alzette river (building…)");
  assert.equal(JSON.stringify(view), before, "the view given is not changed");
  const ids = quick.catalog.map((m) => m.id);
  assert.deepEqual(ids, ["alzette-place-names-streets", "wx-river-clock-2021-07-14-2021-07-16"], "water and buildings left out, names kept");
  const clockItem = quick.catalog[1];
  assert.equal(clockItem.type, "czml");
  assert.equal(clockItem.isPaused, false);
  assert.ok(!clockItem.url, "inline: nothing to fetch");
  const clock = clockItem.czmlData[0].clock;
  assert.equal(clockItem.czmlData[0].id, "document");
  assert.equal(clock.interval, "2021-07-14T00:00:00Z/2021-07-17T00:00:00Z", "from the first midnight to the midnight after the last day");
  assert.equal(clock.currentTime, "2021-07-14T00:00:00Z");
  assert.equal(clock.multiplier, 1800);
  assert.deepEqual(quick.workbench, ["wx-river-clock-2021-07-14-2021-07-16", "alzette-place-names-streets"]);
  assert.deepEqual(quick.timeline, ["wx-river-clock-2021-07-14-2021-07-16"]);
  assert.deepEqual(quick.settings, view.settings);
  // One day.
  assert.equal(core.clockView(view, "2021-07-14", "2021-07-14", 600, "x").catalog.pop().czmlData[0].clock.interval,
    "2021-07-14T00:00:00Z/2021-07-15T00:00:00Z");
});
