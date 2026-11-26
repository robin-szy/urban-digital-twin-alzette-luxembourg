// Water-level control for the Alzette 3D water surface.
//
// Loads a Terria init file from the backend through the URL hash, the same way
// the Digital Twin date selector does. Each new level animates from the previous one.
// By default the water shows the latest gauge readings stored in the database
// (GET /water-level/latest); the HQ10 / HQ100 / HQextreme tick boxes override them while ticked.
// One central panel (draggable, resizable) opens a small movable panel per demonstration;
// their positions and sizes are kept in localStorage.
// The water itself is drawn by this script as an animated Cesium water surface, with flow arrows
// and the estimated velocity on hover (see "3D water surface" below).
// $BACKEND_URL is filled in by entrypoint.sh.
(function () {
  "use strict";

  var BACKEND_URL = "$BACKEND_URL";
  // How often the latest gauge readings are checked again (minutes): set in the Live replay panel.
  var POLL_MINUTES = [1, 5, 15, 30];
  var liveTimer = null;

  // Last keyframe shown: gauge stages in cm, or a scenario name ("hq10") for every gauge.
  var previous = null;
  // What the water shows now ("level/..." or "date/..."), so the buildings
  // checkbox can reload the same view; Terria replaces the whole workbench on each init.
  var currentView = null;
  var showBuildings = false;
  // Place name layers shown ("streets", "other"); listed in every init file like the buildings.
  var showNames = { streets: false, other: false };
  // Terrain appearance, set in the Terrain panel and kept in localStorage: grayscale orthophoto, and
  // the shading of steep terrain (start angle, full-colour angle, colour, opacity).
  var TERRAIN_KEY = "wl-terrain";
  var TERRAIN_DEFAULTS = {
    grayscale: true, slope: true, start: 35, full: 60, colour: "#5a3a1e", opacity: 1,
    // REM view: contour interval (m, one of REM_INTERVALS) and opacity.
    remInterval: 5, remOpacity: 0.7
  };
  var SLOPE_ITEM_ID = "alzette-slope-shading";
  // REM view (height above the river, contours): listed in every init file while shown; off by default.
  var REM_ITEM_ID = "alzette-rem-view";
  var REM_INTERVALS = [1, 2, 5, 10];
  var showRem = false;
  // Gauges used in the water level (live, manual, replay); a disabled one is left out ("x" stage).
  // All enabled by default; at least one stays enabled.
  var GAUGES = [["pf", "Pfaffenthal", "pfaffenthal"], ["he", "Hesperange", "hesperange"], ["li", "Livange", "livange"]];
  var gaugeEnabled = { pf: true, he: true, li: true };
  var SCENARIO_NAMES = ["hq10", "hq100", "hqextreme"];
  // Map layers added by the backend with the layers/<kinds>/ prefix (listed in every init file while
  // shown): rain (on by default, as before), the official flood zones, and the water-fee model's result.
  var mapLayers = { rain: true, "zones-hq10": false, "zones-hq100": false, "zones-hqextreme": false, fees: false,
    // AOI change (2026-11-25): the current reach's centerline and corridor ("Area of interest" category).
    "aoi-centerline": false, "aoi-corridor": false };
  // Refresh number of the day replay's rainfall, raised once its interpolation has been computed.
  var rainVersion = 0;
  // The 3D water surface (and its arrows) shown; hidden without reloading anything.
  var showWater = true;

  // Settings objects kept in localStorage: defaults, with the stored values of the same type.
  function loadSettings(key, defaults) {
    var settings = Object.assign({}, defaults);
    try {
      var stored = JSON.parse(localStorage.getItem(key));
      if (stored && typeof stored === "object") {
        Object.keys(defaults).forEach(function (name) {
          if (typeof stored[name] === typeof defaults[name]) {
            settings[name] = stored[name];
          }
        });
      }
    } catch (e) { /* storage unavailable or invalid: defaults */ }
    return settings;
  }

  function saveSettings(key, settings) {
    try {
      localStorage.setItem(key, JSON.stringify(settings));
    } catch (e) { /* storage unavailable */ }
  }

  function saveTerrainSettings() {
    saveSettings(TERRAIN_KEY, terrainSettings);
  }

  var terrainSettings = loadSettings(TERRAIN_KEY, TERRAIN_DEFAULTS);

  // Look of the animated water (River panel): ripple size (m), ripple period (s), wave strength,
  // frames per second of the animation; and the playback speed of a day replay and the live polling.
  var WATER_KEY = "wl-water";
  var WATER_DEFAULTS = { rippleSize: 7, period: 3, amplitude: 0.8, fps: 30, replaySpeed: 600, pollMin: 5 };
  var waterSettings = loadSettings(WATER_KEY, WATER_DEFAULTS);

  // Show sun: a colour filter over the 3D view from the sun at the time shown (Luxembourg).
  // All its settings are here: the tint of each phase (colour, and strength = opacity 0..1),
  // how long dawn and dusk last (centred on sunrise / sunset) and the crossfade between phases.
  var SUN = {
    lat: 49.61, lon: 6.13,
    twilightMin: 45,
    crossfadeMin: 10,
    // Replay times are the gauge data's stamps: local time (UTC+1 all year) labelled UTC.
    replayUtcOffsetH: 1,
    tints: {
      twilight: { colour: "#ff5a1e", strength: 0.28 },
      day: { colour: "#fff0b4", strength: 0.10 },
      night: { colour: "#060c22", strength: 0.60 }
    }
  };
  var showSun = false;
  // The Sun panel's settings (dawn / dusk length, strength of each tint), kept in localStorage.
  var SUN_KEY = "wl-sun";
  var sunSettings = loadSettings(SUN_KEY, {
    twilightMin: SUN.twilightMin,
    twilight: SUN.tints.twilight.strength,
    day: SUN.tints.day.strength,
    night: SUN.tints.night.strength
  });

  function applySunSettings() {
    SUN.twilightMin = sunSettings.twilightMin;
    ["twilight", "day", "night"].forEach(function (phase) {
      SUN.tints[phase].strength = sunSettings[phase];
    });
  }
  applySunSettings();

  // Sunrise and sunset (ms, UTC) of the solar day nearest a time: the suncalc formulas
  // (Vladimir Agafonkin, BSD-2-Clause) for the sun's upper limb at the horizon (-0.833 degrees).
  function sunTimes(ms, lat, lon) {
    var rad = Math.PI / 180, j1970 = 2440588, j2000 = 2451545, tilt = rad * 23.4397;
    var lw = rad * -lon, phi = rad * lat;
    var d = ms / 86400000 - 0.5 + j1970 - j2000;
    var n = Math.round(d - 0.0009 - lw / (2 * Math.PI));
    var ds = 0.0009 + lw / (2 * Math.PI) + n;
    var m = rad * (357.5291 + 0.98560028 * ds);
    var l = m + rad * (1.9148 * Math.sin(m) + 0.02 * Math.sin(2 * m) + 0.0003 * Math.sin(3 * m)) + rad * 102.9372 + Math.PI;
    var dec = Math.asin(Math.sin(l) * Math.sin(tilt));
    var noon = j2000 + ds + 0.0053 * Math.sin(m) - 0.0069 * Math.sin(2 * l);
    var w = Math.acos((Math.sin(-0.833 * rad) - Math.sin(phi) * Math.sin(dec)) / (Math.cos(phi) * Math.cos(dec)));
    var set = j2000 + 0.0009 + (w + lw) / (2 * Math.PI) + n + 0.0053 * Math.sin(m) - 0.0069 * Math.sin(2 * l);
    var rise = noon - (set - noon);
    function toMs(j) { return (j + 0.5 - j1970) * 86400000; }
    return { sunrise: toMs(rise), sunset: toMs(set) };
  }

  function hexRgb(hex) {
    return [1, 3, 5].map(function (i) { return parseInt(hex.slice(i, i + 2), 16); });
  }

  // Tint at a time (ms, UTC): [r, g, b, alpha], crossfading linearly between the phases.
  function sunTint(ms) {
    var minute = 60000;
    var half = SUN.twilightMin * minute / 2;
    var fade = Math.min(SUN.crossfadeMin, SUN.twilightMin) * minute / 2;
    var keys = [];
    [-86400000, 0, 86400000].forEach(function (shift) {
      var times = sunTimes(ms + shift, SUN.lat, SUN.lon);
      var r = times.sunrise, s = times.sunset;
      keys.push([r - half - fade, "night"], [r - half + fade, "twilight"], [r + half - fade, "twilight"],
        [r + half + fade, "day"], [s - half - fade, "day"], [s - half + fade, "twilight"],
        [s + half - fade, "twilight"], [s + half + fade, "night"]);
    });
    function tint(phase) {
      var t = SUN.tints[phase];
      return hexRgb(t.colour).concat([t.strength]);
    }
    for (var i = 0; i + 1 < keys.length; i++) {
      if (ms >= keys[i][0] && ms < keys[i + 1][0]) {
        var a = tint(keys[i][1]), b = tint(keys[i + 1][1]);
        var f = (ms - keys[i][0]) / (keys[i + 1][0] - keys[i][0]);
        return a.map(function (value, k) { return value + f * (b[k] - value); });
      }
    }
    return tint("night");
  }

  // The time shown: Terria's clock during a day replay, now otherwise.
  function shownTime(t) {
    if (ui.mode && ui.mode.textContent === "REPLAY" && t && t.timelineClock) {
      var now = t.timelineClock.currentTime;
      return now.constructor.toDate(now).getTime() - SUN.replayUtcOffsetH * 3600000;
    }
    return Date.now();
  }

  // The filter: a coloured layer over the 3D view that takes no clicks or hovers.
  var sunLayer = null;

  function updateSun() {
    var t = findTerria();
    var canvas = t && t.cesium && t.cesium.scene && t.cesium.scene.canvas;
    if (!canvas) {
      return;
    }
    if (!sunLayer) {
      sunLayer = document.createElement("div");
      sunLayer.id = "water-level-control-sun";
      sunLayer.style.cssText = "position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none;" +
        "transition:background-color 0.4s linear";
    }
    if (sunLayer.parentNode !== canvas.parentNode) {
      canvas.parentNode.appendChild(sunLayer);
    }
    sunLayer.style.display = showSun ? "" : "none";
    if (showSun) {
      var c = sunTint(shownTime(t));
      sunLayer.style.backgroundColor = "rgba(" + Math.round(c[0]) + "," + Math.round(c[1]) + "," + Math.round(c[2]) + "," +
        c[3].toFixed(3) + ")";
    }
  }
  // Shading of steep terrain over the orthophoto; listed in every init file too. On by default.
  var showSlope = terrainSettings.slope;
  // Active flood scenario, and the level to return to when it is unticked.
  var scenario = null;
  var baseLevel = null;
  // True while the water follows the latest gauge readings.
  var live = true;
  var liveLevel = null;
  var ui = {};
  // Gauge stages of the day being replayed, shown in the inputs as Terria's clock plays it.
  var replayStages = null;
  // The day being replayed (YYYY-MM-DD), reloaded when a gauge is enabled or disabled.
  var replayDate = null;
  var terria = null;

  // names/<kinds>/ path prefix that adds the place name layers to an init file. It is in the path:
  // Terria loads a URL from its hash only when it ends in .json.
  function namesPrefix() {
    var kinds = Object.keys(showNames).filter(function (kind) { return showNames[kind]; });
    var layerKinds = Object.keys(mapLayers).filter(function (kind) { return mapLayers[kind]; }).map(function (kind) {
      if (kind === "fees" && feesVersion) {
        return "fees." + feesVersion;  // a new result (or reach) gets a new init URL
      }
      return kind === "rain" && rainVersion ? "rain." + rainVersion : kind;
    });
    // AOI fix (2026-11-26): the current reach is always part of the init URL ("aoi.<reach_id>";
    // the backend ignores kinds it does not know). Without it a reach switch often produced the
    // SAME URL as before: setting location.hash to an unchanged value does nothing, so Terria
    // kept the old reach until some other toggle (e.g. River centerline) changed the URL.
    // A new URL per reach also stops Terria from reusing its cached init file of another reach.
    if (aoiReach) {
      layerKinds.push("aoi." + aoiReach);
    }
    return (layerKinds.length ? "layers/" + layerKinds.join(",") + "/" : "") +
      (showSlope ? "slope/" : "") + (showRem ? "rem/" : "") + (kinds.length ? "names/" + kinds.join(",") + "/" : "");
  }

  // Stage of one gauge in an init path: the scenario name, or x for a gauge disabled or without a value.
  function stageText(value, key) {
    if (SCENARIO_NAMES.indexOf(value) >= 0) {
      return value;
    }
    return !gaugeEnabled[key] || value === null || value === undefined || value === "" ? "x" : String(value);
  }

  // Path suffix of a day replay that leaves the disabled gauges out.
  function withoutSuffix() {
    var names = GAUGES.filter(function (g) { return !gaugeEnabled[g[0]]; }).map(function (g) { return g[2]; });
    return names.length ? "/without/" + names.join(",") : "";
  }

  // AOI change (Robin, 2026-11-25): Terria replaces the WHOLE workbench with the list in each
  // init file it loads, and every panel click (water level, HQ scenario, a layer box) loads a
  // new one. So a layer the user had added from "Explore data" (e.g. the river centerline)
  // vanished on the next click. Before each init, the layers the init does not manage are
  // remembered and put back once Terria has replaced the workbench. The init's own items all
  // have ids starting with "alzette-" (water, 3D buildings, flood zones, fees, rain, names, REM).
  var restoreTimer = null;

  function keepUserLayers() {
    var t = findTerria();
    if (!t || !t.workbench) {
      return;
    }
    var before = (t.workbench.items || []).slice();
    var kept = before.filter(function (item) {
      return !(typeof item.uniqueId === "string" && item.uniqueId.indexOf("alzette-") === 0);
    });
    if (!kept.length) {
      return;
    }
    clearInterval(restoreTimer);
    var tries = 0;
    restoreTimer = setInterval(function () {
      var items = t.workbench.items || [];
      // Wait until Terria has applied the new init (the workbench list changed), at most ~20 s.
      var replaced = items.length !== before.length || items.some(function (item, i) { return item !== before[i]; });
      if (!replaced && ++tries < 100) {
        return;
      }
      clearInterval(restoreTimer);
      kept.forEach(function (item) {
        if ((t.workbench.items || []).indexOf(item) < 0 && typeof t.workbench.add === "function") {
          t.workbench.add(item);
        }
      });
    }, 200);
  }

  function loadInit(path) {
    currentView = path;
    var prefix = showBuildings ? "buildings/" : "";
    keepUserLayers();  // AOI change: see above
    window.location.hash = BACKEND_URL + "/terria-water-init/" + namesPrefix() + prefix + path + ".json";
  }

  // A gauge disabled, or without a stage (Livange without a recent reading), is sent as x: the
  // backend interpolates between the other gauges and holds the outermost ones' level beyond them.
  function animateTo(pfaffenthal, hesperange, livange) {
    var next = { pf: stageText(pfaffenthal, "pf"), he: stageText(hesperange, "he"), li: stageText(livange, "li") };
    var pf = previous ? previous.pf + "," + next.pf : next.pf;
    var he = previous ? previous.he + "," + next.he : next.he;
    var li = previous ? previous.li + "," + next.li : next.li;
    previous = next;
    loadInit("level/" + pf + "/" + he + "/" + li);
  }

  // Enable or disable a gauge: the water, building colours and velocity follow at once.
  function setGaugeEnabled(key, enabled, box) {
    var others = GAUGES.filter(function (g) { return g[0] !== key && gaugeEnabled[g[0]]; });
    if (!enabled && !others.length) {
      box.checked = true;
      ui.gaugeNote.textContent = "At least one gauge must stay enabled: the water level is interpolated between the enabled gauges.";
      return;
    }
    if (!enabled && !replayDate && baseLevel && others.every(function (g) { return stageText(baseLevel[g[0]], g[0]) === "x"; })) {
      box.checked = true;
      ui.gaugeNote.textContent = "The other enabled gauges have no stage: enter one first.";
      return;
    }
    ui.gaugeNote.textContent = "";
    gaugeEnabled[key] = enabled;
    ui[key].style.opacity = enabled ? "" : "0.45";
    if (replayDate) {
      // The same day, from the time shown: only the gauges used change.
      keepReplayTime();
      loadInit("date/" + replayDate + withoutSuffix());
    } else if (!scenario && baseLevel) {
      // A scenario does not depend on the gauges; unticking it returns to the level with these gauges.
      animateTo(baseLevel.pf, baseLevel.he, baseLevel.li);
    }
  }

  // Replay time to return to once the replay reloaded with other gauges (Terria restarts its clock).
  var pendingReplayTime = null;

  function keepReplayTime() {
    var t = findTerria();
    if (!t || !t.timelineClock) {
      return;
    }
    var now = t.timelineClock.currentTime;
    pendingReplayTime = { ms: now.constructor.toDate(now).getTime(), from: water.itemId, until: Date.now() + 15000 };
  }

  // A gauge level chosen by hand, or the live one; unticks any scenario.
  function applyLevel(pfaffenthalCm, hesperangeCm, livangeCm) {
    replayStages = null;
    replayDate = null;
    baseLevel = { pf: pfaffenthalCm, he: hesperangeCm, li: livangeCm };
    setScenarioBoxes(null);
    scenario = null;
    setMode(live ? "LIVE" : "MANUAL");
    animateTo(pfaffenthalCm, hesperangeCm, livangeCm);
    updateReportLink();
  }

  function setScenario(name) {
    replayStages = null;
    replayDate = null;
    setScenarioBoxes(name);
    scenario = name;
    updateReportLink();
    setMode(name ? name.toUpperCase() : live ? "LIVE" : "MANUAL");
    if (name) {
      animateTo(name, name, name);
    } else if (baseLevel) {
      animateTo(baseLevel.pf, baseLevel.he, baseLevel.li);
    }
  }

  function setScenarioBoxes(name) {
    ui.hq10.checked = name === "hq10";
    ui.hq100.checked = name === "hq100";
    ui.hqextreme.checked = name === "hqextreme";
  }

  function updateReportLink() {
    if (scenario) {
      ui.report.href = BACKEND_URL + "/flood-report/" + scenario + ".html";
      ui.report.textContent = "Open the " + scenario.toUpperCase() + " flood report \u2197";
      ui.report.style.display = "block";
    } else {
      ui.report.style.display = "none";
    }
    showDamage(scenario);
  }

  // Estimated building damage of a scenario (eddie_flood_costs: HQ10 and HQ100 only).
  function showDamage(name) {
    ui.damage.textContent = "";
    if (SCENARIO_NAMES.indexOf(name) < 0) {
      return;
    }
    fetch(BACKEND_URL + "/flood-costs/summary/" + name + ".json")
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (scenario !== name) {
          return;
        }
        if (data.status !== "success") {
          ui.damage.textContent = "Damage estimate unavailable.";
          return;
        }
        var euro = new Intl.NumberFormat("en-IE", { maximumFractionDigits: 0 });
        ui.damage.textContent = "Estimated building damage (provisional, 2010 €): " +
          (data.total_eur2010 === null ? "unknown" : "€" + euro.format(data.total_eur2010)) + ", " +
          data.positive_damage_buildings + " buildings damaged; " + data.unknown_damage_buildings +
          " buildings without an estimate. Click a building for its own estimate.";
      })
      .catch(function () { ui.damage.textContent = "Damage estimate unavailable."; });
  }

  // Load the view shown again with the layers chosen now: the current level without animating it
  // again, or the day replayed from the time shown.
  function reloadView() {
    if (replayDate) {
      keepReplayTime();
      loadInit(currentView);
    } else if (previous) {
      loadInit("level/" + previous.pf + "/" + previous.he + "/" + previous.li);
    } else if (currentView) {
      loadInit(currentView);
    } else {
      keepUserLayers();  // AOI change: user-added layers survive the new init
      window.location.hash = BACKEND_URL + "/terria-water-init/" + namesPrefix() +
        (showBuildings ? "buildings" : "names") + ".json";
    }
    notifyChange();
  }

  function toggleBuildings(checked) {
    showBuildings = checked;
    reloadView();
  }

  function toggleNames(kind, checked) {
    if (kind === "slope") {
      showSlope = checked;
    } else if (kind === "rem") {
      showRem = checked;
    } else {
      showNames[kind] = checked;
    }
    reloadView();
  }

  function toggleMapLayer(kind, checked) {
    mapLayers[kind] = checked;
    if (kind === "rain" && checked && replayDate) {
      prepareRain(replayDate);
    } else if (kind === "rain") {
      refreshLayerStatus();
    }
    reloadView();
  }

  // ---------------------------------------------------------------------------
  // 3D water surface. The backend computes the water of each level on a grid over the LiDAR DEM
  // (/water-surface-level/<level>.json); it is drawn here as a Cesium primitive with an animated
  // water material, which Terria's CZML layers cannot use. The water item Terria loads from an
  // init file only carries the timeline: the level shown follows Terria's clock.
  // ---------------------------------------------------------------------------
  var WATER_ITEM_PREFIX = "alzette-3d-water-surface";
  var WATER_ID = "alzette-water";
  var WATER_NORMALS = "build/TerriaJS/build/Cesium/build/Assets/Textures/waterNormals.jpg";
  // Camera height above the water (m) up to which each level of detail of the arrows is shown.
  var ARROW_SHOW_UP_TO_M = [1200, 5000, Infinity];
  // Length (m) of the arrows of each level of detail, and their height above the water.
  var ARROW_LENGTH_M = [7, 26, 100];
  var ARROW_LIFT_M = 0.12;
  var WATER_HEIGHT_M = 250;
  // Built water primitives kept, to switch back to them without rebuilding.
  var MAX_BUILT_LEVELS = 16;
  var showArrows = true;
  var water = { url: null, plan: null, data: {}, built: {}, order: [], shown: null, visible: true, geometries: {} };
  var cesiumApi = null;

  // Terria bundles Cesium without a global; find the classes needed in its webpack modules,
  // by their own static members (the bundle is minified).
  function cesiumClasses() {
    if (cesiumApi) {
      return cesiumApi;
    }
    var chunks = window.webpackChunkterriajs_map;
    if (!chunks) {
      return null;
    }
    var require = null;
    chunks.push([["wl-water-" + Date.now()], {}, function (r) { require = r; }]);
    if (!require || !require.m) {
      return null;
    }
    var wanted = {
      Material: ["WaterType", function (v) { return typeof v === "function" && v.WaterType && v.fromType; }],
      Primitive: ["getGeometryInstanceAttributes", function (v) {
        return typeof v === "function" && v.prototype && v.prototype.getGeometryInstanceAttributes &&
          v.prototype.update && !v.isSupported && !v.initializeTerrainHeights;
      }],
      GeometryInstance: ["westHemisphereGeometry", function (v) {
        var source = typeof v === "function" ? Function.prototype.toString.call(v) : "";
        return source.indexOf("westHemisphereGeometry") >= 0 && source.indexOf("modelMatrix") >= 0 &&
          !(v.prototype && v.prototype.update);
      }],
      Geometry: ["computeNumberOfVertices", function (v) { return typeof v === "function" && v.computeNumberOfVertices; }],
      BoundingSphere: ["fromVertices", function (v) { return typeof v === "function" && v.fromVertices && v.fromPoints; }],
      // The base Appearance: a material with our own shaders (Terria's bundle has no MaterialAppearance).
      Appearance: ["getDefaultRenderState", function (v) {
        return typeof v === "function" && v.getDefaultRenderState && !v.VERTEX_FORMAT &&
          v.prototype && v.prototype.getFragmentShaderSource;
      }]
    };
    var found = {};
    Object.keys(require.m).forEach(function (id) {
      var source;
      try {
        source = Function.prototype.toString.call(require.m[id]);
      } catch (e) {
        return;
      }
      Object.keys(wanted).forEach(function (name) {
        if (found[name] || source.indexOf(wanted[name][0]) < 0) {
          return;
        }
        var exports;
        try {
          exports = require(id);
        } catch (e) {
          return;
        }
        Object.keys(exports || {}).forEach(function (key) {
          var value;
          try {
            value = exports[key];
          } catch (e) {
            return;
          }
          if (!found[name] && wanted[name][1](value)) {
            found[name] = value;
          }
        });
      });
    });
    if (Object.keys(wanted).every(function (name) { return found[name]; })) {
      cesiumApi = found;
    }
    return cesiumApi;
  }

  var WATER_VS = [
    "in vec3 position3DHigh;",
    "in vec3 position3DLow;",
    "in vec3 normal;",
    "in vec2 st;",
    "in vec2 flow;",
    "in float batchId;",
    "out vec3 v_positionEC;",
    "out vec3 v_normalEC;",
    "out vec2 v_st;",
    "out vec2 v_flow;",
    "void main() {",
    "  vec4 p = czm_computePosition();",
    "  v_positionEC = (czm_modelViewRelativeToEye * p).xyz;",
    "  v_normalEC = czm_normal * normal;",
    "  v_st = st;",
    "  v_flow = flow;",
    "  gl_Position = czm_modelViewProjectionRelativeToEye * p;",
    "}"
  ].join("\n");

  // The flow (m/s, east and north) reaches the material through materialInput.str; the tangent
  // frame is east / north / up, the axes st and the flow are given in.
  var WATER_FS = [
    "in vec3 v_positionEC;",
    "in vec3 v_normalEC;",
    "in vec2 v_st;",
    "in vec2 v_flow;",
    "void main() {",
    "  vec3 positionToEyeEC = -v_positionEC;",
    "  vec3 normalEC = normalize(v_normalEC);",
    "#ifdef FACE_FORWARD",
    "  normalEC = faceforward(normalEC, vec3(0.0, 0.0, 1.0), -normalEC);",
    "#endif",
    "  vec3 axisEC = czm_normal * vec3(0.0, 0.0, 1.0);",
    "  vec3 eastEC = normalize(cross(axisEC, normalEC));",
    "  vec3 northEC = cross(normalEC, eastEC);",
    "  czm_materialInput materialInput;",
    "  materialInput.normalEC = normalEC;",
    "  materialInput.positionToEyeEC = positionToEyeEC;",
    "  materialInput.st = v_st;",
    "  materialInput.str = vec3(v_flow, 0.0);",
    "  materialInput.tangentToEyeMatrix = mat3(eastEC, northEC, normalEC);",
    "  czm_material material = czm_getMaterial(materialInput);",
    "  out_FragColor = czm_phong(normalize(positionToEyeEC), material, czm_lightDirectionEC);",
    "}"
  ].join("\n");

  // Moving normal map: two layers advected with the flow (flow map), crossfaded so the ripples
  // drift downstream without stretching, faster where the estimated velocity is higher; a fine
  // layer keeps still water alive. Fresnel transparency and a specular sun reflection.
  var WATER_MATERIAL = [
    // Material textures clamp at their edge: repeat the normal map by hand, with the gradients of
    // the unwrapped coordinates so the mipmaps leave no seams.
    "vec3 rippleNormal(vec2 p) {",
    "  return textureGrad(normalMap, fract(p), dFdx(p), dFdy(p)).rgb * 2.0 - 1.0;",
    "}",
    "czm_material czm_getMaterial(czm_materialInput materialInput) {",
    "  czm_material material = czm_getDefaultMaterial(materialInput);",
    "  vec2 flow = materialInput.str.xy;",
    "  float phase0 = fract(time / period);",
    "  float phase1 = fract(time / period + 0.5);",
    "  float blend = abs(1.0 - 2.0 * phase0);",
    "  vec2 uv = materialInput.st / rippleSize;",
    "  vec2 travel = flow * period / rippleSize;",
    "  vec3 n0 = rippleNormal(uv - travel * phase0);",
    "  vec3 n1 = rippleNormal(uv - travel * phase1 + vec2(0.37, 0.61));",
    "  vec3 fine = rippleNormal(uv * 3.3 + vec2(time * 0.011, -time * 0.008));",
    "  vec2 slope = mix(n0.xy, n1.xy, blend) + 0.35 * fine.xy;",
    "  vec3 normalTangent = normalize(vec3(slope * amplitude, 1.0));",
    "  material.normal = normalize(materialInput.tangentToEyeMatrix * normalTangent);",
    "  vec3 toEye = normalize(materialInput.positionToEyeEC);",
    "  float fresnel = pow(1.0 - clamp(dot(material.normal, toEye), 0.0, 1.0), 3.0);",
    // Sky reflected by each ripple: lighter where it tilts towards the light, darker away from it.
    "  float sky = clamp(0.5 + 1.6 * dot(normalTangent.xy, vec2(0.6, 0.8)), 0.0, 1.0);",
    "  material.diffuse = mix(vec3(0.07, 0.24, 0.35), vec3(0.34, 0.52, 0.62), sky);",
    "  material.diffuse = mix(material.diffuse, vec3(0.62, 0.74, 0.82), 0.6 * fresnel);",
    "  material.alpha = mix(0.72, 0.95, fresnel);",
    "  material.specular = 0.9;",
    "  material.shininess = 80.0;",
    "  return material;",
    "}"
  ].join("\n");

  var ARROW_VS = [
    "in vec3 position3DHigh;",
    "in vec3 position3DLow;",
    "in vec3 normal;",
    "in float batchId;",
    "out vec3 v_positionEC;",
    "out vec3 v_normalEC;",
    "void main() {",
    "  vec4 p = czm_computePosition();",
    "  v_positionEC = (czm_modelViewRelativeToEye * p).xyz;",
    "  v_normalEC = czm_normal * normal;",
    "  gl_Position = czm_modelViewProjectionRelativeToEye * p;",
    "}"
  ].join("\n");

  var ARROW_FS = [
    "in vec3 v_positionEC;",
    "in vec3 v_normalEC;",
    "void main() {",
    "  czm_materialInput materialInput;",
    "  materialInput.normalEC = normalize(v_normalEC);",
    "  materialInput.positionToEyeEC = -v_positionEC;",
    "  czm_material material = czm_getMaterial(materialInput);",
    "  out_FragColor = vec4(material.diffuse + material.emission, material.alpha);",
    "}"
  ].join("\n");

  var ARROW_MATERIAL = [
    "czm_material czm_getMaterial(czm_materialInput materialInput) {",
    "  czm_material material = czm_getDefaultMaterial(materialInput);",
    "  material.diffuse = vec3(0.13, 0.42, 0.98);",
    "  material.emission = vec3(0.05, 0.16, 0.38);",
    "  material.alpha = 1.0;",
    "  return material;",
    "}"
  ].join("\n");

  function decodeBase64(text, Type) {
    var binary = atob(text);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) {
      bytes[i] = binary.charCodeAt(i);
    }
    return new Type(bytes.buffer);
  }

  // WGS84 longitude / latitude of a mesh node (fractional row / column): bilinear between the
  // grid corners (exact to the millimetre over the DEM).
  function nodeLonLat(grid, row, col) {
    var u = col / grid.cols;
    var v = row / grid.rows;
    var c = grid.corners;
    return [
      (1 - v) * ((1 - u) * c[0][0] + u * c[1][0]) + v * ((1 - u) * c[2][0] + u * c[3][0]),
      (1 - v) * ((1 - u) * c[0][1] + u * c[1][1]) + v * ((1 - u) * c[2][1] + u * c[3][1])
    ];
  }

  // Inverse of nodeLonLat (the grid is near enough to affine for picking).
  function lonLatNode(grid, lon, lat) {
    var c = grid.corners;
    var a = c[1][0] - c[0][0], b = c[2][0] - c[0][0];
    var d = c[1][1] - c[0][1], e = c[2][1] - c[0][1];
    var x = lon - c[0][0], y = lat - c[0][1];
    var det = a * e - b * d;
    return { row: (a * y - d * x) / det * grid.rows, col: (e * x - b * y) / det * grid.cols };
  }

  var WGS84_A = 6378137.0;
  var WGS84_E2 = 6.69437999014e-3;

  function toEcef(lon, lat, height, out, offset) {
    var lambda = lon * Math.PI / 180, phi = lat * Math.PI / 180;
    var sinPhi = Math.sin(phi), cosPhi = Math.cos(phi);
    var n = WGS84_A / Math.sqrt(1 - WGS84_E2 * sinPhi * sinPhi);
    out[offset] = (n + height) * cosPhi * Math.cos(lambda);
    out[offset + 1] = (n + height) * cosPhi * Math.sin(lambda);
    out[offset + 2] = (n * (1 - WGS84_E2) + height) * sinPhi;
  }

  function toLonLat(position) {
    var x = position.x, y = position.y, z = position.z;
    var p = Math.sqrt(x * x + y * y);
    var lat = Math.atan2(z, p * (1 - WGS84_E2));
    for (var i = 0; i < 4; i++) {
      var sinLat = Math.sin(lat);
      var n = WGS84_A / Math.sqrt(1 - WGS84_E2 * sinLat * sinLat);
      lat = Math.atan2(z + WGS84_E2 * n * sinLat, p);
    }
    return [Math.atan2(y, x) * 180 / Math.PI, lat * 180 / Math.PI];
  }

  function geometry(Cesium, attributes, indices, positions) {
    return new Cesium.Geometry({
      attributes: attributes,
      indices: indices,
      primitiveType: 4, // TRIANGLES
      boundingSphere: Cesium.BoundingSphere.fromVertices(positions)
    });
  }

  // Not closed (seen from both sides); the water lit on the side facing the camera.
  function appearance(Cesium, material, vertexShader, fragmentShader, translucent) {
    var result = new Cesium.Appearance({
      material: material,
      translucent: translucent,
      closed: false,
      vertexShaderSource: vertexShader,
      fragmentShaderSource: fragmentShader,
      renderState: Cesium.Appearance.getDefaultRenderState(translucent, false)
    });
    result.faceForward = true;
    return result;
  }

  function attribute(values, size, isDouble) {
    return { componentDatatype: isDouble ? 5130 : 5126, componentsPerAttribute: size, values: values };
  }

  // One level's water mesh and arrows, as Cesium primitives.
  // Mesh cells of one layer (row-major indices), from their indices or one bit per cell.
  function layerCells(layer) {
    if (layer.cell_index) {
      return decodeBase64(layer.cell_index, Uint32Array);
    }
    var bits = decodeBase64(layer.cells, Uint8Array);
    var list = [];
    for (var byteIndex = 0; byteIndex < bits.length; byteIndex++) {
      if (bits[byteIndex]) {
        for (var bit = 0; bit < 8; bit++) {
          if (bits[byteIndex] & (128 >> bit)) {
            list.push(byteIndex * 8 + bit);
          }
        }
      }
    }
    return Uint32Array.from(list);
  }

  // One level's water mesh and arrows, as Cesium primitives. The mesh has two layers (floodplain,
  // channel), each with its own nodes, drawn as one surface.
  // Mesh of the water sections (/water-surface-sections.json), shared by every level: vertices as
  // mesh-grid columns / rows, and triangles.
  function decodeGeometry(data) {
    return {
      count: data.vertices,
      points: decodeBase64(data.points, Float32Array),
      triangles: decodeBase64(data.triangles, Uint32Array),
      buckets: null
    };
  }

  // Vertices of the section mesh by square of BUCKET_CELLS mesh cells, for picking.
  var BUCKET_CELLS = 8;

  function nearestSectionVertex(geometry, row, col) {
    if (!geometry.buckets) {
      geometry.buckets = new Map();
      for (var i = 0; i < geometry.count; i++) {
        var bucketKey = Math.floor(geometry.points[2 * i] / BUCKET_CELLS) + "," + Math.floor(geometry.points[2 * i + 1] / BUCKET_CELLS);
        var list = geometry.buckets.get(bucketKey);
        if (!list) {
          geometry.buckets.set(bucketKey, list = []);
        }
        list.push(i);
      }
    }
    var best = -1, bestDistance = Infinity;
    var bucketCol = Math.floor(col / BUCKET_CELLS), bucketRow = Math.floor(row / BUCKET_CELLS);
    for (var dc = -1; dc <= 1; dc++) {
      for (var dr = -1; dr <= 1; dr++) {
        (geometry.buckets.get((bucketCol + dc) + "," + (bucketRow + dr)) || []).forEach(function (vertex) {
          var distance = Math.pow(geometry.points[2 * vertex] - col, 2) + Math.pow(geometry.points[2 * vertex + 1] - row, 2);
          if (distance < bestDistance) {
            bestDistance = distance;
            best = vertex;
          }
        });
      }
    }
    return best;
  }

  // AOI change (Robin, 2026-11-25): the 3D water (live levels and HQ scenarios) is computed over
  // the whole DEM, but is drawn only inside the CURRENT reach's 500 m corridor - the same area as
  // the 3D buildings and the module results. aoiClip holds that corridor (lon/lat rings, from
  // the AOI module); buildLevel keeps only the triangles whose three corners lie inside it, and
  // only the flow arrows inside it. Without a corridor (AOI module absent), nothing is cut.
  var aoiClip = { reach: null, rings: null, bbox: null };

  function loadAoiClip(reach) {
    if (!reach || reach === aoiClip.reach) {
      return Promise.resolve(false);
    }
    return fetch(BACKEND_URL + "/aoi/reach/" + reach + "/variants.geojson?variant_key=corridor_500")
      .then(function (response) { return response.json(); })
      .then(function (collection) {
        var rings = [];
        (collection.features || []).forEach(function (feature) {
          var g = feature.geometry || {};
          var polygons = g.type === "Polygon" ? [g.coordinates] : g.type === "MultiPolygon" ? g.coordinates : [];
          polygons.forEach(function (polygon) { polygon.forEach(function (ring) { rings.push(ring); }); });
        });
        if (!rings.length) {
          return false;
        }
        var bbox = [Infinity, Infinity, -Infinity, -Infinity];
        rings.forEach(function (ring) {
          ring.forEach(function (p) {
            bbox = [Math.min(bbox[0], p[0]), Math.min(bbox[1], p[1]), Math.max(bbox[2], p[0]), Math.max(bbox[3], p[1])];
          });
        });
        aoiClip = { reach: reach, rings: rings, bbox: bbox };
        return true;
      })
      .catch(function () { return false; });
  }

  // Even-odd point in polygon over all rings (outer rings and holes alike).
  function insideAoi(lon, lat) {
    if (!aoiClip.rings) {
      return true;
    }
    var b = aoiClip.bbox;
    if (lon < b[0] || lon > b[2] || lat < b[1] || lat > b[3]) {
      return false;
    }
    var inside = false;
    aoiClip.rings.forEach(function (ring) {
      for (var i = 0, j = ring.length - 1; i < ring.length; j = i++) {
        var yi = ring[i][1], yj = ring[j][1];
        if ((yi > lat) !== (yj > lat) && lon < (ring[j][0] - ring[i][0]) * (lat - yi) / (yj - yi) + ring[i][0]) {
          inside = !inside;
        }
      }
    });
    return inside;
  }

  // A new corridor: drop every built water level so they are drawn again, cut to it.
  function rebuildWaterForAoi() {
    var scene = water.scene;
    Object.keys(water.built).forEach(function (key) {
      if (scene) {
        destroyLevel(scene, water.built[key]);
      }
    });
    water.built = {};
    water.order = [];
    water.shown = null;
  }

  function buildLevel(Cesium, grid, data, sectionGeometry) {
    var rows = grid.rows, cols = grid.cols, stride = cols + 1;
    var layers = (data.layers || [data]).map(function (layer) {
      if (layer.geometry === "sections") {
        return {
          sections: sectionGeometry,
          height: decodeBase64(layer.height, Float32Array),
          flow: decodeBase64(layer.flow, Int16Array),
          depth: decodeBase64(layer.depth, Uint16Array),
          km: decodeBase64(layer.km, Uint16Array),
          river: decodeBase64(layer.river, Uint8Array)
        };
      }
      var cells = layerCells(layer);
      var vertexOf = new Int32Array((rows + 1) * stride).fill(-1);
      cells.forEach(function (cell) {
        var r = Math.floor(cell / cols), c = cell % cols, node = r * stride + c;
        vertexOf[node] = vertexOf[node + 1] = vertexOf[node + stride] = vertexOf[node + stride + 1] = 0;
      });
      var nodes = [];
      for (var node = 0; node < vertexOf.length; node++) {
        if (vertexOf[node] === 0) {
          vertexOf[node] = nodes.length;
          nodes.push(node);
        }
      }
      return {
        cells: cells,
        vertexOf: vertexOf,
        nodes: Uint32Array.from(nodes),
        height: decodeBase64(layer.height, Float32Array),
        flow: decodeBase64(layer.flow, Int16Array),
        depth: decodeBase64(layer.depth, Uint16Array),
        km: decodeBase64(layer.km, Uint16Array),
        river: decodeBase64(layer.river, Uint8Array)
      };
    });
    var count = 0, triangles = 0;
    layers.forEach(function (layer) {
      layer.first = count;
      count += layer.sections ? layer.sections.count : layer.nodes.length;
      triangles += layer.sections ? layer.sections.triangles.length / 3 : 2 * layer.cells.length;
    });
    var positions = new Float64Array(count * 3);
    var normals = new Float32Array(count * 3);
    var st = new Float32Array(count * 2);
    var flow = new Float32Array(count * 2);
    var indices = new Uint32Array(triangles * 3);
    var used = 0;
    var inAoi = new Uint8Array(count);  // AOI change: vertex inside the reach corridor
    layers.forEach(function (layer) {
      var vertices = layer.sections ? layer.sections.count : layer.nodes.length;
      for (var i = 0; i < vertices; i++) {
        var k = layer.first + i;
        var row, col;
        if (layer.sections) {
          col = layer.sections.points[2 * i];
          row = layer.sections.points[2 * i + 1];
        } else {
          row = Math.floor(layer.nodes[i] / stride);
          col = layer.nodes[i] % stride;
        }
        var lonLat = nodeLonLat(grid, row, col);
        inAoi[k] = insideAoi(lonLat[0], lonLat[1]) ? 1 : 0;  // AOI change
        toEcef(lonLat[0], lonLat[1], layer.height[i], positions, 3 * k);
        var lambda = lonLat[0] * Math.PI / 180, phi = lonLat[1] * Math.PI / 180;
        normals[3 * k] = Math.cos(phi) * Math.cos(lambda);
        normals[3 * k + 1] = Math.cos(phi) * Math.sin(lambda);
        normals[3 * k + 2] = Math.sin(phi);
        st[2 * k] = col * grid.cell_m;
        st[2 * k + 1] = (rows - row) * grid.cell_m;
        flow[2 * k] = layer.flow[2 * i] / 1000;
        flow[2 * k + 1] = layer.flow[2 * i + 1] / 1000;
      }
      if (layer.sections) {
        for (var j = 0; j < layer.sections.triangles.length; j++) {
          indices[used + j] = layer.first + layer.sections.triangles[j];
        }
        used += layer.sections.triangles.length;
        layer.height = null;
        return;
      }
      layer.cells.forEach(function (cell) {
        var r = Math.floor(cell / cols), c = cell % cols, n = r * stride + c, v = layer.vertexOf, o = layer.first;
        var a = o + v[n], b = o + v[n + 1], d = o + v[n + stride], e = o + v[n + stride + 1];
        indices.set([a, d, b, b, d, e], used);
        used += 6;
      });
      // Not needed once the mesh is built.
      layer.vertexOf = null;
      layer.cells = null;
      layer.height = null;
    });

    // AOI change: keep only the triangles with all three corners inside the reach corridor.
    if (aoiClip.rings) {
      var kept = 0;
      for (var tri = 0; tri < used; tri += 3) {
        if (inAoi[indices[tri]] && inAoi[indices[tri + 1]] && inAoi[indices[tri + 2]]) {
          indices[kept] = indices[tri];
          indices[kept + 1] = indices[tri + 1];
          indices[kept + 2] = indices[tri + 2];
          kept += 3;
        }
      }
      indices = indices.slice(0, kept);
    }

    var material = new Cesium.Material({
      fabric: {
        uniforms: {
          normalMap: WATER_NORMALS, rippleSize: waterSettings.rippleSize, period: waterSettings.period,
          amplitude: waterSettings.amplitude, time: 0.0
        },
        source: WATER_MATERIAL
      },
      translucent: true
    });
    var surface = new Cesium.Primitive({
      geometryInstances: new Cesium.GeometryInstance({
        geometry: geometry(Cesium, {
          position: attribute(positions, 3, true),
          normal: attribute(normals, 3),
          st: attribute(st, 2),
          flow: attribute(flow, 2)
        }, indices, positions),
        id: WATER_ID
      }),
      appearance: appearance(Cesium, material, WATER_VS, WATER_FS, true),
      asynchronous: false,
      compressVertices: false,
      vertexCacheOptimize: false,
      interleave: false
    });

    var arrows = (data.arrows || []).map(function (arrowList, detail) {
      // AOI change: only the flow arrows inside the reach corridor.
      arrowList = arrowList.filter(function (arrow) {
        var at = nodeLonLat(grid, arrow[1], arrow[0]);
        return insideAoi(at[0], at[1]);
      });
      if (!arrowList.length) {
        return null;
      }
      var length = ARROW_LENGTH_M[Math.min(detail, ARROW_LENGTH_M.length - 1)];
      // Arrow outline in metres (x downstream, y left): tip, back left, notch, back right.
      var shape = [[0.5, 0], [-0.5, 0.42], [-0.22, 0], [-0.5, -0.42]];
      var arrowPositions = new Float64Array(arrowList.length * 12);
      var arrowNormals = new Float32Array(arrowList.length * 12);
      var arrowIndices = new Uint32Array(arrowList.length * 6);
      arrowList.forEach(function (arrow, i) {
        var cosA = Math.cos(arrow[3]), sinA = Math.sin(arrow[3]);
        shape.forEach(function (point, j) {
          var east = (point[0] * cosA - point[1] * sinA) * length;
          var north = (point[0] * sinA + point[1] * cosA) * length;
          var lonLat = nodeLonLat(grid, arrow[1] - north / grid.cell_m, arrow[0] + east / grid.cell_m);
          toEcef(lonLat[0], lonLat[1], arrow[2] + ARROW_LIFT_M, arrowPositions, 12 * i + 3 * j);
          var lambda = lonLat[0] * Math.PI / 180, phi = lonLat[1] * Math.PI / 180;
          arrowNormals.set([Math.cos(phi) * Math.cos(lambda), Math.cos(phi) * Math.sin(lambda), Math.sin(phi)], 12 * i + 3 * j);
        });
        arrowIndices.set([0, 1, 2, 0, 2, 3].map(function (v) { return 4 * i + v; }), 6 * i);
      });
      return new Cesium.Primitive({
        geometryInstances: new Cesium.GeometryInstance({
          geometry: geometry(Cesium, {
            position: attribute(arrowPositions, 3, true),
            normal: attribute(arrowNormals, 3)
          }, arrowIndices, arrowPositions),
          id: WATER_ID + "-arrows"
        }),
        appearance: appearance(Cesium, new Cesium.Material({ fabric: { source: ARROW_MATERIAL } }), ARROW_VS, ARROW_FS, false),
        asynchronous: false,
        compressVertices: false,
        vertexCacheOptimize: false,
        interleave: false
      });
    });

    return {
      surface: surface,
      material: material,
      arrows: arrows,
      // Channel first: it is drawn over the floodplain where both meet.
      layers: layers.slice().reverse(),
      stats: data.stats
    };
  }

  function destroyLevel(scene, level) {
    [level.surface].concat(level.arrows).forEach(function (primitive) {
      if (primitive) {
        scene.primitives.remove(primitive);
      }
    });
  }

  function addLevel(scene, level) {
    [level.surface].concat(level.arrows).forEach(function (primitive) {
      if (primitive) {
        primitive.show = false;
        scene.primitives.add(primitive);
      }
    });
  }

  // The water of the current workbench item, at the level of Terria's current time.
  function updateWater() {
    var t = findTerria();
    var scene = t && t.cesium && t.cesium.scene;
    if (!scene) {
      return;
    }
    var item = (t.workbench.items || []).find(function (i) {
      return typeof i.uniqueId === "string" && i.uniqueId.indexOf(WATER_ITEM_PREFIX) === 0;
    });
    var url = item && item.url ? item.url.replace("/water-surface.czml", "/water-surface-mesh.json") : null;
    if (pendingReplayTime && item && item.uniqueId !== pendingReplayTime.from) {
      // The replay reloaded with other gauges: once Terria restarted its clock, back to the time shown before.
      var clockNow = t.timelineClock.currentTime;
      if (clockNow.constructor.toDate(clockNow).getTime() < pendingReplayTime.ms - 60000) {
        t.timelineClock.currentTime = clockNow.constructor.fromDate(new Date(pendingReplayTime.ms));
        pendingReplayTime = null;
      }
    }
    if (pendingReplayTime && Date.now() > pendingReplayTime.until) {
      pendingReplayTime = null;
    }
    var itemId = item ? item.uniqueId : null;
    if (itemId && itemId !== water.itemId && replayDate && t.timelineClock) {
      // A day replay plays at the speed chosen in the Data replay panel (Terria takes the CZML's otherwise).
      t.timelineClock.multiplier = waterSettings.replaySpeed;
    }
    water.itemId = itemId;
    if (url !== water.url) {
      water.url = url;
      water.plan = null;
      if (url) {
        fetch(url)
          .then(function (response) { return response.json(); })
          .then(function (plan) {
            if (water.url !== url || plan.status !== "success") {
              return;
            }
            plan.frames.forEach(function (frame) { frame.ms = Date.parse(frame.time); });
            // The section mesh first (once), then the final level, then every level in the order they are shown.
            var geometryReady = !plan.geometry || water.geometries[plan.geometry] ? Promise.resolve() :
              fetch(plan.geometry)
                .then(function (response) { return response.json(); })
                .then(function (data) { water.geometries[plan.geometry] = decodeGeometry(data); });
            geometryReady.then(function () {
              water.plan = plan;
              return [plan.frames[plan.frames.length - 1]].concat(plan.frames).reduce(function (chain, frame) {
                return chain.then(function () { return loadLevelData(frame.level, plan.levels[frame.level]); });
              }, Promise.resolve());
            });
          })
          .catch(function () { /* keep the water shown */ });
      }
    }

    var Cesium = cesiumClasses();
    var plan = water.plan;
    var level = null;
    var next = null;
    if (plan && Cesium) {
      var now = t.timelineClock.currentTime;
      var ms = now.constructor.toDate(now).getTime();
      var key = plan.frames[0].level;
      for (var i = 0; i < plan.frames.length && plan.frames[i].ms <= ms; i++) {
        key = plan.frames[i].level;
      }
      next = builtLevel(Cesium, scene, plan.grid, key);
      // A new level becomes ready once drawn: until then the previous one stays shown with it.
      level = next && next.surface.ready ? next : water.shown || next;
    }
    var visible = !!item && item.show !== false && showWater;
    // Camera height above the water (the river lies at about WATER_HEIGHT_M).
    var height = scene.camera.positionCartographic.height - WATER_HEIGHT_M;
    Object.keys(water.built).forEach(function (builtKey) {
      var built = water.built[builtKey];
      var current = built === level && visible;
      built.surface.show = (current || built === next) && visible;
      built.arrows.forEach(function (arrows, detail) {
        if (arrows) {
          arrows.show = current && showArrows && height <= ARROW_SHOW_UP_TO_M[detail] &&
            (detail === 0 || height > ARROW_SHOW_UP_TO_M[detail - 1]);
        }
      });
    });
    if (level) {
      water.shown = level;
    }
    water.grid = plan ? plan.grid : water.grid;
    water.scene = scene;
    water.animate = !!level && visible;
    applyTerrain(t, scene);
  }

  // Slope shading style of the tile urls: start-full-colour, e.g. 35-60-5a3a1e.
  function slopeStyle() {
    var start = Math.max(0, Math.min(89, Number(terrainSettings.start) || 0));
    var full = Math.max(start + 1, Math.min(90, Number(terrainSettings.full) || 90));
    var colour = /^#[0-9a-fA-F]{6}$/.test(terrainSettings.colour) ? terrainSettings.colour : TERRAIN_DEFAULTS.colour;
    return start + "-" + full + "-" + colour.slice(1).toLowerCase();
  }

  // Grayscale base map (the lowest imagery layer) and the slope shading layer's style and opacity.
  function applyTerrain(t, scene) {
    var layers = scene.imageryLayers;
    if (layers && layers.length) {
      var saturation = terrainSettings.grayscale ? 0 : 1;
      var base = layers.get(0);
      if (base.saturation !== saturation) {
        base.saturation = saturation;
        scene.requestRender();
      }
    }
    var item = (t.workbench.items || []).find(function (i) { return i.uniqueId === SLOPE_ITEM_ID; });
    if (item && item.setTrait) {
      var url = BACKEND_URL + "/slope-shading/" + slopeStyle() + "/";
      if (item.url !== url) {
        item.setTrait("user", "url", url);
      }
      if (item.opacity !== terrainSettings.opacity) {
        item.setTrait("user", "opacity", terrainSettings.opacity);
      }
    }
    // REM view: the precomputed tiles of the contour interval chosen, at the opacity chosen.
    var rem = (t.workbench.items || []).find(function (i) { return i.uniqueId === REM_ITEM_ID; });
    if (rem && rem.setTrait) {
      var interval = REM_INTERVALS.indexOf(terrainSettings.remInterval) >= 0 ? terrainSettings.remInterval : TERRAIN_DEFAULTS.remInterval;
      var remUrl = BACKEND_URL + "/rem-view/" + interval + "/";
      if (rem.url !== remUrl) {
        rem.setTrait("user", "url", remUrl);
      }
      if (rem.opacity !== terrainSettings.remOpacity) {
        rem.setTrait("user", "opacity", terrainSettings.remOpacity);
      }
    }
  }

  // Fly the camera to a gauge (GET /water-level/gauges.json), looking downstream along the river.
  var gaugePositions = null;
  var GAUGE_VIEW = { pitch: -0.55, range: 520, durationS: 2.5 };

  function flyToGauge(name) {
    var t = findTerria();
    var scene = t && t.cesium && t.cesium.scene;
    var Cesium = cesiumClasses();
    if (!scene || !Cesium) {
      return;
    }
    var positions = gaugePositions ? Promise.resolve(gaugePositions) :
      fetch(BACKEND_URL + "/water-level/gauges.json")
        .then(function (response) { return response.json(); })
        .then(function (data) { gaugePositions = data.gauges; return gaugePositions; });
    positions.then(function (gauges) {
      var gauge = (gauges || []).find(function (g) { return g.name === name; });
      if (!gauge) {
        return;
      }
      var centre = [0, 0, 0];
      toEcef(gauge.lon, gauge.lat, gauge.height_m, centre, 0);
      scene.camera.flyToBoundingSphere(new Cesium.BoundingSphere({ x: centre[0], y: centre[1], z: centre[2] }, 30), {
        offset: { heading: gauge.heading_deg * Math.PI / 180, pitch: GAUGE_VIEW.pitch, range: GAUGE_VIEW.range },
        duration: GAUGE_VIEW.durationS
      });
    }).catch(function () { /* backend unreachable: stay */ });
  }

  // Terria renders only when something changes; while the water is shown, render it at about
  // waterSettings.fps frames per second so its ripples move, with the look chosen in the River panel.
  var lastWaterFrame = 0;

  function animateWater(now) {
    if (water.animate && water.shown && now - lastWaterFrame >= 1000 / waterSettings.fps) {
      lastWaterFrame = now;
      var uniforms = water.shown.material.uniforms;
      uniforms.time = (now / 1000) % 3600;
      uniforms.rippleSize = waterSettings.rippleSize;
      uniforms.period = waterSettings.period;
      uniforms.amplitude = waterSettings.amplitude;
      water.scene.requestRender();
    }
    window.requestAnimationFrame(animateWater);
  }

  function loadLevelData(key, url) {
    if (water.data[key]) {
      return Promise.resolve(water.data[key]);
    }
    return fetch(url)
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (data.layers || data.cells || data.cell_index) {
          water.data[key] = data;
        }
        return data;
      })
      .catch(function () { return null; });
  }

  function builtLevel(Cesium, scene, grid, key) {
    if (water.built[key]) {
      return water.built[key];
    }
    var data = water.data[key];
    if (!data) {
      return null;
    }
    var level;
    var sectionGeometry = water.plan && water.plan.geometry ? water.geometries[water.plan.geometry] : null;
    try {
      level = buildLevel(Cesium, grid, data, sectionGeometry);
    } catch (e) {
      console.error("Water level " + key + " could not be drawn", e);
      delete water.data[key];
      return null;
    }
    addLevel(scene, level);
    water.built[key] = level;
    water.order.push(key);
    while (water.order.length > MAX_BUILT_LEVELS) {
      var old = water.order.shift();
      if (water.built[old] !== water.shown) {
        destroyLevel(scene, water.built[old]);
        delete water.built[old];
      } else {
        water.order.push(old);
      }
    }
    return level;
  }

  // Estimated velocity, depth and river km of the water under the pointer.
  function waterInfo(scene, x, y) {
    var level = water.shown, grid = water.grid;
    if (!level || !grid || !scene.pickPositionSupported) {
      return null;
    }
    var position = scene.pickPosition({ x: x, y: y });
    if (!position) {
      return null;
    }
    var lonLat = toLonLat(position);
    var at = lonLatNode(grid, lonLat[0], lonLat[1]);
    var stride = grid.cols + 1;
    var best = -1, bestDistance = Infinity, bestLayer = null;
    level.layers.forEach(function (layer) {
      if (bestLayer) {
        return;
      }
      if (layer.sections) {
        best = nearestSectionVertex(layer.sections, at.row, at.col);
        if (best >= 0) {
          bestLayer = layer;
        }
        return;
      }
      [[0, 0], [0, 1], [1, 0], [1, 1]].forEach(function (offset) {
        var row = Math.floor(at.row) + offset[0], col = Math.floor(at.col) + offset[1];
        var node = row * stride + col;
        var lo = 0, hi = layer.nodes.length - 1;
        while (lo <= hi) {
          var mid = (lo + hi) >> 1;
          if (layer.nodes[mid] < node) { lo = mid + 1; } else if (layer.nodes[mid] > node) { hi = mid - 1; } else { break; }
        }
        if (lo <= hi) {
          var distance = Math.pow(row - at.row, 2) + Math.pow(col - at.col, 2);
          if (distance < bestDistance) {
            bestDistance = distance;
            best = (lo + hi) >> 1;
          }
        }
      });
      if (best >= 0) {
        bestLayer = layer;
      }
    });
    if (!bestLayer) {
      return null;
    }
    var found = bestLayer;
    var speed = Math.sqrt(Math.pow(found.flow[2 * best], 2) + Math.pow(found.flow[2 * best + 1], 2)) / 1000;
    var river = grid.rivers[found.river[best]] || "River";
    return "Estimated velocity ≈ " + speed.toFixed(2) + " m/s (estimate: Manning, n = " +
      level.stats.manning_n + ")\nDepth " + (found.depth[best] / 100).toFixed(2) + " m · " + river + " km " +
      (found.km[best] / 100).toFixed(2);
  }

  // Full name of a place name point, or the estimated velocity of the water, under the pointer.
  var HOVER_INTERVAL_MS = 100;

  function setupHover() {
    var t = findTerria();
    var viewer = t && (t.cesium || t.currentViewer);
    var scene = viewer && viewer.scene;
    if (!scene || !scene.canvas) {
      // Terria (or its 3D view) is not ready yet.
      setTimeout(setupHover, 2000);
      return;
    }
    var tooltip = element("div", "wl-tooltip");
    tooltip.style.display = "none";
    document.body.appendChild(tooltip);
    var last = 0;
    scene.canvas.addEventListener("mousemove", function (event) {
      var now = Date.now();
      if (now - last < HOVER_INTERVAL_MS) {
        return;
      }
      last = now;
      var rect = scene.canvas.getBoundingClientRect();
      var picked;
      try {
        picked = scene.pick({ x: event.clientX - rect.left, y: event.clientY - rect.top });
      } catch (e) {
        picked = null;
      }
      var entity = picked && picked.id;
      var info = picked && picked.id === WATER_ID ?
        waterInfo(scene, event.clientX - rect.left, event.clientY - rect.top) : null;
      if (info) {
        tooltip.textContent = info;
        tooltip.style.whiteSpace = "pre";
        tooltip.style.left = event.clientX + 12 + "px";
        tooltip.style.top = event.clientY + 12 + "px";
        tooltip.style.display = "block";
      } else if (entity && typeof entity.id === "string" && entity.id.indexOf("placename-") === 0) {
        tooltip.textContent = entity.name;
        tooltip.style.whiteSpace = "nowrap";
        tooltip.style.left = event.clientX + 12 + "px";
        tooltip.style.top = event.clientY + 12 + "px";
        tooltip.style.display = "block";
      } else {
        tooltip.style.display = "none";
      }
    });
    scene.canvas.addEventListener("mouseleave", function () {
      tooltip.style.display = "none";
    });
  }

  // Terria keeps no global handle; find it through the React props of its UI.
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

  // Stage of one gauge at a time, interpolated between its readings.
  function stageAt(values, times, ms) {
    if (values[0] === null) {
      return "";
    }
    var i = 0;
    while (i + 1 < times.length && times[i + 1] <= ms) {
      i++;
    }
    if (i + 1 >= times.length || ms <= times[i]) {
      return values[i].toFixed(1);
    }
    var fraction = (ms - times[i]) / (times[i + 1] - times[i]);
    return (values[i] + fraction * (values[i + 1] - values[i])).toFixed(1);
  }

  function showReplayStages() {
    var t = replayStages && findTerria();
    if (!t) {
      return;
    }
    var now = t.timelineClock.currentTime;
    var ms = now.constructor.toDate(now).getTime();
    ui.pf.value = stageAt(replayStages.pfaffenthal_cm, replayStages.ms, ms);
    ui.he.value = stageAt(replayStages.hesperange_cm, replayStages.ms, ms);
    ui.li.value = stageAt(replayStages.livange_cm, replayStages.ms, ms);
    ui.replayInfo.textContent = "Replaying " + new Date(ms).toISOString().replace("T", " ").slice(0, 16);
  }

  // Latest gauge readings from the database; the default water level.
  function refreshLive() {
    fetch(BACKEND_URL + "/water-level/latest")
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (data.status !== "success") {
          ui.liveInfo.textContent = "No live gauge data stored yet.";
          if (!currentView && !previous && showSlope) {
            // Nothing else loads an init file: show the slope shading on its own.
            toggleNames("slope", true);
          }
          return;
        }
        var changed = !liveLevel || liveLevel.pf !== data.pfaffenthal_cm || liveLevel.he !== data.hesperange_cm ||
          liveLevel.li !== data.livange_cm;
        liveLevel = { pf: data.pfaffenthal_cm, he: data.hesperange_cm, li: data.livange_cm };
        ui.liveInfo.textContent =
          "Latest reading " + data.datetime.replace("T", " ").slice(0, 16) +
          ": Pfaffenthal " + data.pfaffenthal_cm + ", Hesperange " + data.hesperange_cm +
          (data.livange_cm === null ? "" : ", Livange " + data.livange_cm) + " cm";
	// When the user is viewing historical data,
	// live gauge updates must not replace the map.

	if (window.location.hash.includes("/terria-date-init/")) {
	  setMode("HISTORICAL");
	  return;
	}
        if (!live || !changed) {
          return;
        }
        ui.pf.value = data.pfaffenthal_cm;
        ui.he.value = data.hesperange_cm;
        ui.li.value = data.livange_cm === null ? "" : data.livange_cm;
        if (!currentView && !entered &&
            !window.location.hash.includes("/terria-water-init/")) {
          return;
        }
        if (scenario) {
          // The scenario stays on screen; unticking it returns to this level.
          baseLevel = liveLevel;
        } else {
          applyLevel(data.pfaffenthal_cm, data.hesperange_cm, data.livange_cm);
        }
      })
      .catch(function () {
        ui.liveInfo.textContent = "Live gauge data unavailable.";
      });
  }

  var MODE_COLOURS = { LIVE: "#2e9d5b", MANUAL: "#4a7bd0", REPLAY: "#8a63c9", HQ10: "#e08a1e", HQ100: "#d0412f", HQEXTREME: "#8e1b1b" };

  // Badge in the panel header: what the water shows now.
  function setMode(mode) {
    ui.mode.textContent = mode;
    ui.mode.style.background = MODE_COLOURS[mode] || "#555";
    notifyChange();
  }

  function scheduleLive() {
    if (liveTimer) {
      clearInterval(liveTimer);
    }
    // The rainfall status is checked again too, so "no live rainfall" goes away once data is published.
    liveTimer = setInterval(function () {
      refreshLive();
      refreshLayerStatus();
    }, waterSettings.pollMin * 60 * 1000);
  }

  // Back to the latest stored gauge readings.
  function goLive() {
    live = true;
    liveLevel = null;
    refreshLive();
    refreshLayerStatus();
  }

  // Replay one day of stored gauge readings. Resolves to { ok, message } once the replay is loaded
  // (or refused: a day without stored readings would send Terria to a water surface that cannot load).
  function replayDay(selected) {
    return fetch(BACKEND_URL + "/terria-water-init/date/" + selected + ".json")
      .then(function (response) {
        if (!response.ok) {
          return response.json().then(function (data) {
            var message = data.message || "No gauge readings stored for " + selected + ".";
            ui.replayInfo.textContent = message;
            return { ok: false, message: message };
          });
        }
        ui.replayInfo.textContent = "Replaying " + selected + ".";
        ui.replayDay.value = selected;
        live = false;
        previous = null;
        setScenarioBoxes(null);
        scenario = null;
        updateReportLink();
        replayDate = selected;
        rainVersion = 0;
        setMode("REPLAY");
        loadInit("date/" + selected + withoutSuffix());
        if (mapLayers.rain) {
          prepareRain(selected);
        }
        return fetch(BACKEND_URL + "/water-level/day/" + selected + ".json")
          .then(function (stagesResponse) { return stagesResponse.json(); })
          .then(function (data) {
            if (data.status === "success") {
              data.ms = data.times.map(function (time) { return Date.parse(time); });
              replayStages = data;
            }
            return { ok: true, message: "Replaying " + selected + "." };
          });
      })
      .catch(function () {
        ui.replayInfo.textContent = "Could not reach the backend.";
        return { ok: false, message: "Could not reach the backend." };
      });
  }

  // The rainfall of a replayed day is interpolated on request (POST /load-precipitation-period);
  // once computed, the replay is loaded again with the new rainfall, from the time shown.
  var rainInfo = "";

  function setRainInfo(text) {
    rainInfo = text;
    if (ui.rainInfo) {
      ui.rainInfo.textContent = text;
    }
    notifyChange();
  }

  // Fetch ASTA rain observations for a period (eddie_alzette_ingest); resolves to the finished task.
  function fetchRainObservations(startIso, endIso) {
    return fetch(BACKEND_URL + "/alzette-ingest/precipitation", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: "backfill", start: startIso, end: endIso })
    })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        return data.status === "processing" ? waitForTask(data.task_id) : { status: "error", message: data.message };
      });
  }

  function prepareRain(day, fetched) {
    fetch(BACKEND_URL + "/load-precipitation-period", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ date: day })
    })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (data.status === "cached") {
          setRainInfo("Rainfall of " + day + " shown (" + data.timestamps + " time steps).");
        } else if (data.status === "no_data" && !fetched) {
          // Not fetched yet: get the day's observations from ASTA, then interpolate them.
          setRainInfo("Fetching the rain observations of " + day + "...");
          return fetchRainObservations(day + "T00:00:00Z", day + "T23:59:59Z").then(function (task) {
            if (replayDate !== day) {
              return;
            }
            if (task.status !== "finished") {
              setRainInfo("The rain observations of " + day + " could not be fetched.");
              return;
            }
            prepareRain(day, true);
          });
        } else if (data.status === "no_data") {
          setRainInfo("No rain observations exist for " + day + ".");
        } else if (data.status === "processing") {
          setRainInfo("Computing the rainfall of " + day + "...");
          return waitForTask(data.task_id).then(function (task) {
            if (replayDate !== day) {
              return;
            }
            if (task.status !== "finished") {
              setRainInfo("The rainfall of " + day + " could not be computed.");
              return;
            }
            setRainInfo("Rainfall of " + day + " shown.");
            rainVersion += 1;
            if (mapLayers.rain) {
              reloadView();
            }
          });
        } else {
          setRainInfo(data.message || "The rainfall could not be loaded.");
        }
      })
      .catch(function () {
        setRainInfo("Could not reach the backend for the rainfall.");
      });
  }

  // Which data-dependent layers GeoServer serves now (GET /map-layers/status.json): the backend
  // leaves the others out of the init files, and the panels say why.
  var layerStatus = null;
  var feesVersion = null;

  // AOI change (2026-11-20): the AOI layers on the workbench (centerline, corridors, buffer,
  // watersheds, ...) show the twin's CURRENT reach. Terria loads a GeoJSON layer once, so when
  // another reach is selected, each such layer gets the current reach's URL (a new URL makes
  // Terria load it again). The selector's own result layers ("AOI corridor - <river> km ...")
  // name one reach on purpose and are left alone.
  var AOI_LAYER_URL = /\/aoi\/(?:reach\/[^/]+|current)\/variants\.geojson\?variant_key=([^&]+)/;

  function followAoiReach(reach) {
    var t = findTerria();
    if (!reach || !t || !t.workbench) {
      return;
    }
    (t.workbench.items || []).forEach(function (item) {
      var match = typeof item.url === "string" && item.url.match(AOI_LAYER_URL);
      if (!match || !item.setTrait || /^AOI corridor - /.test(item.name || "")) {
        return;
      }
      var url = BACKEND_URL + "/aoi/reach/" + reach + "/variants.geojson?variant_key=" + match[1];
      if (item.url !== url) {
        item.setTrait("user", "url", url);
        // The startup group's names carry the km range of the reach it opened on.
        var name = (item.name || "").replace(/\s*\([^()]* km [^()]*\)$/, "");
        if (name !== item.name) {
          item.setTrait("user", "name", name);
        }
      }
    });
  }

  // ---------------------------------------------------------------------------
  // AOI change (Robin, 2026-11-25): after a reach switch the map shows the NEW reach's results
  // only, and says which are still being computed.
  //  * GET /aoi/current/status.json lists the modules recomputed for the current reach
  //    (population, water fees, flood depths and costs) with their state.
  //  * The banner below shows "computing..." per module until it is done, then the notes of
  //    partial results (e.g. flood costs only for part of the reach), then hides itself.
  //  * When the reach changes, the view is loaded again: the 3D buildings and the water-fee
  //    layer carry the reach in their id and URL, so Terria fetches the new reach's ones.
  //  * When a module finishes, every GeoServer result layer on the workbench (Input Layers:
  //    flood depths, flood costs, population; the water fees) gets a new "aoi" parameter,
  //    which makes Terria request its tiles again instead of showing cached old ones. The
  //    database views behind them already show the current reach only.
  // ---------------------------------------------------------------------------
  var aoiReach = null;
  var aoiStatusVersion = null;
  var aoiDoneTimer = null;
  var aoiFastPoll = null;

  function aoiBanner() {
    var banner = document.getElementById("aoi-status-banner");
    if (!banner) {
      banner = document.createElement("div");
      banner.id = "aoi-status-banner";
      banner.style.cssText = "position:fixed;top:12px;left:50%;transform:translateX(-50%);z-index:10000;" +
        "background:rgba(17,24,39,.92);color:#f9fafb;padding:8px 14px;border-radius:8px;font:13px/1.4 sans-serif;" +
        "max-width:min(640px,90vw);box-shadow:0 2px 10px rgba(0,0,0,.35);display:none;cursor:pointer";
      banner.title = "Click to hide";
      banner.addEventListener("click", function () { banner.style.display = "none"; });
      document.body.appendChild(banner);
    }
    return banner;
  }

  // Timing (Robin, 2026-11-26): each module shows how long it has been computing, counted up
  // every second between two polls; once done, its result line and how long it took. When
  // everything is done the banner stays 6 s, then hides. elapsed_s comes from the database
  // (started_at / finished_at stamped by the worker), so the browser clock does not matter.
  var aoiLastData = null;
  var aoiLastFetch = 0;
  var aoiTicker = null;

  function aoiDuration(seconds) {
    if (seconds === null || seconds === undefined) {
      return "";
    }
    var s = Math.max(0, Math.round(seconds));
    return s < 60 ? s + " s" : Math.floor(s / 60) + " min " + (s % 60 < 10 ? "0" : "") + (s % 60) + " s";
  }

  function renderAoiStatus() {
    var data = aoiLastData;
    if (!data) {
      return;
    }
    var banner = aoiBanner();
    var area = data.river ? data.river + " km " + data.km_start + "–" + data.km_end : data.reach_id;
    var sincePoll = (Date.now() - aoiLastFetch) / 1000;
    var lines = (data.modules || []).map(function (m) {
      var mark;
      if (m.state === "computing") {
        if (m.started === false) {
          mark = "⏳ waiting…";        // queued behind the task before it in its chain
        } else {
          var running = m.elapsed_s === null || m.elapsed_s === undefined ? null : m.elapsed_s + sincePoll;
          mark = "⏳ computing…" + (running === null ? "" : " " + aoiDuration(running));
        }
      } else {
        mark = m.state === "failed" ? "⚠️ failed" : m.state === "partial" ? "◐ partial" :
          m.state === "none" ? "– none" : "✓ ready";
        if (m.elapsed_s !== null && m.elapsed_s !== undefined) {
          mark += " in " + aoiDuration(m.elapsed_s);
        }
      }
      var details = [m.summary, m.note].filter(function (x) { return x; });
      return "<div>" + m.label + ": <b>" + mark + "</b>" +
        details.map(function (d) { return "<div style=\"opacity:.8;font-size:12px\">" + d + "</div>"; }).join("") +
        "</div>";
    });
    banner.innerHTML = "<div style=\"font-weight:600;margin-bottom:2px\">Area of interest: " + area + "</div>" +
      (lines.length ? lines.join("") : "<div>No module results recorded for this area yet.</div>");
  }

  function showAoiStatus(data) {
    aoiLastData = data;
    aoiLastFetch = Date.now();
    renderAoiStatus();
    var banner = aoiBanner();
    clearTimeout(aoiDoneTimer);
    banner.style.display = "block";
    if (data.computing && !aoiTicker) {
      aoiTicker = setInterval(renderAoiStatus, 1000);   // the running clocks
    } else if (!data.computing) {
      if (aoiTicker) {
        clearInterval(aoiTicker);
        aoiTicker = null;
      }
      // Everything finished: keep the results up for 6 s, then hide.
      aoiDoneTimer = setTimeout(function () { banner.style.display = "none"; }, 6000);
    }
  }

  function refreshResultLayers(version) {
    var t = findTerria();
    if (!t || !t.workbench) {
      return;
    }
    (t.workbench.items || []).forEach(function (item) {
      if (item.type === "wms" && typeof item.url === "string" && item.url.indexOf("/geoserver/input_layers") >= 0 &&
          item.setTrait) {
        var parameters = Object.assign({}, item.parameters || {}, { aoi: version });
        item.setTrait("user", "parameters", parameters);
      }
    });
  }

  function refreshAoiStatus() {
    return fetch(BACKEND_URL + "/aoi/current/status.json")
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (data.status !== "success") {
          return;
        }
        // The water is drawn inside the current reach's corridor only (see aoiClip): load the
        // corridor on the first status and on every reach change, then redraw the water.
        loadAoiClip(data.reach_id).then(function (loaded) {
          if (loaded) {
            rebuildWaterForAoi();
          }
        });
        var reachChanged = aoiReach !== null && data.reach_id !== aoiReach;
        var changed = aoiStatusVersion !== null && data.version !== aoiStatusVersion;
        var firstStatus = aoiReach === null;
        aoiReach = data.reach_id;
        aoiStatusVersion = data.version;
        // The panel's "Area of interest" category: the current area, and the built list's selection.
        if (ui.aoiCurrent) {
          ui.aoiCurrent.textContent = "Current area: " +
            (data.river ? data.river + " km " + data.km_start + "–" + data.km_end : data.reach_id);
        }
        if ((reachChanged || firstStatus) && ui.loadAoiReaches) {
          ui.loadAoiReaches();
        }
        if (reachChanged) {
          followAoiReach(data.reach_id);
          reloadView();  // new 3D buildings and water-fee layer for the new reach
        }
        if (reachChanged || changed) {
          refreshResultLayers(data.version);
          showAoiStatus(data);
        } else if (data.computing) {
          showAoiStatus(data);
        }
        // Poll every 4 s while something is computing, back to the normal 15 s afterwards.
        if (data.computing && !aoiFastPoll) {
          aoiFastPoll = setInterval(refreshAoiStatus, 4000);
        } else if (!data.computing && aoiFastPoll) {
          clearInterval(aoiFastPoll);
          aoiFastPoll = null;
        }
      })
      .catch(function () { /* backend unreachable: keep what is shown */ });
  }

  function refreshLayerStatus() {
    refreshAoiStatus();  // AOI change: the current reach's module states (see above)
    return fetch(BACKEND_URL + "/map-layers/status.json")
      .then(function (response) { return response.json(); })
      .then(function (data) {
        var rainArrived = !!(layerStatus && !layerStatus.rain_live && data.rain_live);
        layerStatus = data;
        followAoiReach(data.aoi_reach);
        // AOI change: the water-fee result follows the selected reach and is recomputed in the
        // background; when it changes, load the layer again so the map shows the new one.
        if (data.fees_version && data.fees_version !== feesVersion) {
          var feesChanged = feesVersion !== null;
          feesVersion = data.fees_version;
          if (feesChanged && mapLayers.fees) {
            reloadView();
          }
        }
        ui.feesLayer.disabled = !data.fees;
        ui.feesLayer.parentNode.title = data.fees ? "" : "Run the water-fee model first (Water-fee model panel).";
        if (!replayDate) {
          // The note is shown only while there is really no live rainfall; it is cleared as soon as there is.
          setRainInfo(mapLayers.rain && !data.rain_live ? "No live rainfall is published yet." : "");
          if (rainArrived && mapLayers.rain) {
            // The rainfall was published since the view was loaded: load it again (under a new
            // refresh number, so Terria fetches the init file again) to show it.
            rainVersion += 1;
            reloadView();
          }
        }
      })
      .catch(function () { /* backend unreachable: the layers stay as they are */ });
  }

  // Follow a Celery task (GET /digital-twin-task-status) until it finishes, at most ~5 minutes.
  function waitForTask(taskId) {
    var tries = 0;
    return new Promise(function (resolve) {
      (function poll() {
        fetch(BACKEND_URL + "/digital-twin-task-status/" + taskId)
          .then(function (response) { return response.json(); })
          .then(function (task) {
            if (task.status === "finished" || task.status === "error" || ++tries > 200) {
              resolve(task);
            } else {
              setTimeout(poll, 1500);
            }
          })
          .catch(function () { resolve({ status: "error", message: "Could not reach the backend." }); });
      })();
    });
  }

  // ---------------------------------------------------------------------------
  // Shared state and public API (window.AlzetteTwin), used by the Casual mode (casual-ui.js) and the
  // mode selector (twin-mode.js). Both modes act on the same state: a layer switched in one is
  // switched in the other, and the date shown stays when the mode changes.
  // ---------------------------------------------------------------------------
  var LAYER_NAMES = ["rain", "river", "buildings", "zones", "names"];

  function layerState() {
    return {
      rain: mapLayers.rain,
      river: showWater,
      buildings: showBuildings,
      zones: mapLayers["zones-hq100"],
      names: showNames.streets && showNames.other
    };
  }

  // The in-depth controls showing each shared layer, kept ticked as the layers change.
  var layerBoxes = {};

  function syncBoxes() {
    var state = layerState();
    var boxes = {
      rain: [state.rain], river: [state.river], buildings: [state.buildings], zones: [state.zones],
      streets: [showNames.streets], other: [showNames.other]
    };
    Object.keys(layerBoxes).forEach(function (key) {
      if (boxes[key] && layerBoxes[key].checked !== boxes[key][0]) {
        layerBoxes[key].checked = boxes[key][0];
      }
    });
  }

  var notifyPending = false;

  function notifyChange() {
    if (notifyPending) {
      return;
    }
    notifyPending = true;
    Promise.resolve().then(function () {
      notifyPending = false;
      syncBoxes();
      window.dispatchEvent(new CustomEvent("alzette-twin-change", { detail: twinState() }));
    });
  }

  function clockState() {
    var t = findTerria();
    var clock = t && t.timelineClock;
    if (!clock || !clock.startTime || !clock.stopTime) {
      return null;
    }
    function ms(julian) { return julian.constructor.toDate(julian).getTime(); }
    return {
      start: ms(clock.startTime), stop: ms(clock.stopTime), current: ms(clock.currentTime),
      playing: !!clock.shouldAnimate, multiplier: clock.multiplier
    };
  }

  function twinState() {
    return {
      layers: layerState(),
      mode: ui.mode ? ui.mode.textContent : "LIVE",
      replayDate: replayDate,
      scenario: scenario,
      rainInfo: rainInfo,
      liveInfo: ui.liveInfo ? ui.liveInfo.textContent : "",
      uiMode: uiMode
    };
  }

  function setLayer(name, on) {
    on = !!on;
    if (layerState()[name] === on) {
      return;
    }
    if (name === "rain") {
      toggleMapLayer("rain", on);
    } else if (name === "zones") {
      toggleMapLayer("zones-hq100", on);
    } else if (name === "buildings") {
      toggleBuildings(on);
    } else if (name === "names") {
      showNames.streets = on;
      showNames.other = on;
      reloadView();
    } else if (name === "river") {
      showWater = on;
      updateWater();
      notifyChange();
    }
  }

  // Casual hides the in-depth panels and Terria's side panel; the map, its state and the date stay.
  var uiMode = "indepth";

  function findViewState() {
    var nodes = document.querySelectorAll("body *");
    for (var i = 0; i < nodes.length; i++) {
      var key = Object.keys(nodes[i]).find(function (k) {
        return k.indexOf("__reactInternalInstance$") === 0 || k.indexOf("__reactFiber$") === 0;
      });
      for (var fiber = key && nodes[i][key]; fiber; fiber = fiber["return"]) {
        var props = fiber.memoizedProps;
        if (props && props.viewState && props.viewState.terria) {
          return props.viewState;
        }
      }
    }
    return null;
  }

  // True once a mode was chosen on the entry screen: from then on the live water may replace the
  // start view (before, live readings only update a water view already shown).
  var entered = false;

  function setUiMode(mode) {
    uiMode = mode === "casual" ? "casual" : "indepth";
    if (!entered) {
      entered = true;
      if (!currentView && live && liveLevel && !window.location.hash.includes("/terria-date-init/")) {
        applyLevel(liveLevel.pf, liveLevel.he, liveLevel.li);
      }
    }
    document.body.classList.toggle("twin-casual", uiMode === "casual");
    (function fullScreen(tries) {
      var viewState = findViewState();
      if (!viewState) {
        // Terria's UI is not mounted yet.
        if (tries > 0) {
          setTimeout(function () { fullScreen(tries - 1); }, 500);
        }
        return;
      }
      var wanted = uiMode === "casual";
      if (typeof viewState.setIsMapFullScreen === "function") {
        viewState.setIsMapFullScreen(wanted);
      } else {
        viewState.isMapFullScreen = wanted;
      }
    })(40);
    notifyChange();
  }

  window.AlzetteTwin = {
    LAYER_NAMES: LAYER_NAMES,
    getState: twinState,
    clock: clockState,
    setLayer: setLayer,
    replayDay: replayDay,
    goLive: goLive,
    play: function (on) {
      var t = findTerria();
      if (t && t.timelineClock) {
        t.timelineClock.shouldAnimate = !!on;
        notifyChange();
      }
    },
    seek: function (ms) {
      var t = findTerria();
      var clock = t && t.timelineClock;
      if (clock && clock.currentTime) {
        clock.currentTime = clock.currentTime.constructor.fromDate(new Date(ms));
        if (t.currentViewer && t.currentViewer.notifyRepaintRequired) {
          t.currentViewer.notifyRepaintRequired();
        }
      }
    },
    setUiMode: setUiMode
  };

  var STYLE = [
    ".wl-panel{position:fixed;z-index:1000;box-sizing:border-box;max-width:calc(100vw - 16px);",
    "max-height:calc(100vh - 16px);overflow:auto;resize:both;min-width:180px;min-height:60px;",
    "background:rgba(32,38,52,0.95);color:#e8ecf2;border-radius:8px;",
    "box-shadow:0 4px 16px rgba(0,0,0,0.35);font:13px/1.4 Nunito,\"Segoe UI\",sans-serif}",
    ".wl-panel .wl-head{display:flex;align-items:center;gap:8px;padding:8px 10px 8px 12px;cursor:move;user-select:none;",
    "touch-action:none;border-bottom:1px solid rgba(255,255,255,0.08);position:sticky;top:0;z-index:1;background:rgb(32,38,52)}",
    ".wl-panel .wl-title{font-weight:700;flex:1}",
    ".wl-panel .wl-mode{font-size:11px;font-weight:700;letter-spacing:.05em;padding:2px 8px;border-radius:10px;color:#fff}",
    ".wl-panel .wl-close{flex:0 0 auto;cursor:pointer;border:0;background:none;color:inherit;opacity:.7;",
    "font-size:18px;line-height:1;padding:0 2px}",
    ".wl-panel .wl-close:hover{opacity:1}",
    ".wl-panel .wl-body{padding:4px 12px 8px}",
    ".wl-panel .wl-section{padding:6px 0}",
    ".wl-panel h4{margin:0 0 6px;font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;opacity:.65}",
    ".wl-panel .wl-note{font-size:12px;opacity:.8;margin-top:6px}",
    ".wl-panel .wl-note:empty{display:none}",
    ".wl-panel table{width:100%;border-collapse:collapse}",
    ".wl-panel td{padding:3px 0}",
    ".wl-panel td.wl-km{opacity:.6;font-size:12px;text-align:right;padding-right:8px}",
    ".wl-panel td.wl-input{width:90px}",
    // Casual mode (twin-mode.js) shows its own panel instead of these.
    "body.twin-casual .wl-panel,body.twin-casual #eddie-flood-cost-comparison{display:none!important}",
    ".wl-panel input[type=number],.wl-panel input[type=date],.wl-panel input[type=datetime-local]{width:100%;box-sizing:border-box;",
    "background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.15);border-radius:4px;color:inherit;",
    "padding:3px 6px;font:inherit;color-scheme:dark}",
    ".wl-panel input[type=range]{width:100%;margin:8px 0 6px}",
    ".wl-panel .wl-row{display:flex;gap:6px}",
    ".wl-panel .wl-row button,.wl-panel .wl-body>.wl-section>button{flex:1;cursor:pointer;border:0;border-radius:4px;",
    "padding:6px 8px;font:inherit;font-weight:600;background:#3f6fb5;color:#fff;white-space:nowrap}",
    ".wl-panel .wl-row input[type=date]{flex:1;min-width:0}",
    ".wl-panel .wl-row input[type=date]+button{flex:0 0 auto}",
    ".wl-panel button.wl-secondary{background:rgba(255,255,255,0.12)}",
    ".wl-panel button:hover{filter:brightness(1.12)}",
    ".wl-panel .wl-toggle{display:flex;align-items:center;gap:8px;padding:3px 0;cursor:pointer}",
    ".wl-panel .wl-toggle input{accent-color:#3f6fb5;width:15px;height:15px;margin:0}",
    ".wl-panel a{color:#8cc8ff;display:inline-block;margin-top:6px}",
    ".wl-panel .wl-legend{display:flex;gap:10px;flex-wrap:wrap;font-size:12px;opacity:.85;margin-top:6px}",
    ".wl-panel .wl-swatch{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}",
    ".wl-panel button.wl-go{margin-left:6px;cursor:pointer;border:0;border-radius:4px;padding:0 5px;font:inherit;",
    "line-height:18px;background:rgba(255,255,255,0.12);color:inherit}",
    ".wl-panel input.wl-gauge-use{accent-color:#3f6fb5;width:13px;height:13px;margin:0 0 0 6px;vertical-align:-2px;cursor:pointer}",
    ".wl-panel select{background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.15);border-radius:4px;",
    "color:inherit;padding:2px 4px;font:inherit;color-scheme:dark}",
    ".wl-panel .wl-ramp{height:8px;border-radius:2px;margin-top:6px}",
    // The Digital Twin date box (date-selector.js) inside the Time category: no longer floating.
    ".wl-panel #dt-date-box{position:static;width:auto;padding:2px 0;background:none;box-shadow:none;",
    "border-radius:0;z-index:auto;color:inherit;font:inherit}",
    ".wl-panel #dt-date-box h3{display:none}",
    ".wl-panel #dt-load-btn{cursor:pointer;border:0;border-radius:4px;padding:4px 10px;font:inherit;font-weight:600;",
    "background:#3f6fb5;color:#fff}",
    ".wl-panel #dt-status{font-size:12px}",
    ".wl-panel input[type=color]{width:100%;height:24px;padding:0 2px;border:1px solid rgba(255,255,255,0.15);",
    "border-radius:4px;background:rgba(255,255,255,0.08)}",
    ".wl-panel .wl-category{padding:4px 0}",
    ".wl-panel button.wl-category-head{display:flex;align-items:center;gap:6px;width:100%;cursor:pointer;border:0;",
    "background:none;color:inherit;padding:2px 0;font:inherit;font-size:11px;font-weight:700;letter-spacing:.08em;",
    "text-transform:uppercase;opacity:.65;text-align:left}",
    ".wl-panel button.wl-category-head:hover{opacity:.9}",
    ".wl-panel button.wl-category-head::before{content:\"\\25be\";display:inline-block;width:10px}",
    ".wl-panel button.wl-category-head[aria-expanded=false]::before{content:\"\\25b8\"}",
    ".wl-panel .wl-category-body{padding:2px 0 2px 4px}",
    ".wl-panel .wl-category-body>.wl-row{margin:6px 0 2px}",
    ".wl-panel .wl-field{display:block;margin:4px 0}",
    ".wl-panel .wl-field>span{display:block;font-size:12px;opacity:.8;margin-bottom:2px}",
    ".wl-tooltip{position:fixed;z-index:2000;pointer-events:none;background:rgba(32,38,52,0.95);color:#e8ecf2;",
    "padding:3px 8px;border-radius:4px;font:12px/1.4 Nunito,\"Segoe UI\",sans-serif;white-space:nowrap}"
  ].join("");

  // Positions and sizes of the panels, per panel key, kept between sessions.
  var LAYOUT_KEY = "wl-layout";
  var layout = {};
  var zTop = 1000;

  function loadLayout() {
    try {
      var stored = JSON.parse(localStorage.getItem(LAYOUT_KEY));
      if (stored && typeof stored === "object") {
        layout = stored;
      }
    } catch (e) { /* storage unavailable or invalid: defaults */ }
  }

  function saveLayout() {
    try {
      localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout));
    } catch (e) { /* storage unavailable */ }
  }

  // Keep at least the header on screen, also when the window got smaller since last time.
  function clampLeft(left) {
    return Math.max(0, Math.min(left, window.innerWidth - 60));
  }

  function clampTop(top) {
    return Math.max(0, Math.min(top, window.innerHeight - 40));
  }

  function place(panel, key, defaults) {
    var saved = layout[key] || {};
    panel.style.width = (typeof saved.width === "number" ? saved.width : defaults.width) + "px";
    if (typeof saved.height === "number") {
      panel.style.height = saved.height + "px";
    }
    panel.style.left = clampLeft(typeof saved.left === "number" ? saved.left : defaults.left) + "px";
    panel.style.top = clampTop(typeof saved.top === "number" ? saved.top : defaults.top) + "px";
  }

  // Rendered now: not closed, and not hidden by the Casual mode (whose layout must not be remembered).
  function isShown(panel) {
    return panel.style.display !== "none" && panel.getClientRects().length > 0;
  }

  function remember(panel, key) {
    if (!isShown(panel)) {
      return;
    }
    layout[key] = { left: panel.offsetLeft, top: panel.offsetTop, width: panel.offsetWidth };
    // The height is kept only once resized by hand; until then it follows the content.
    if (panel.style.height) {
      layout[key].height = panel.offsetHeight;
    }
    saveLayout();
  }

  function bringToFront(panel) {
    zTop += 1;
    panel.style.zIndex = zTop;
  }

  // Drag the panel by its header.
  function draggable(panel, handle, key) {
    handle.addEventListener("pointerdown", function (event) {
      if (event.button !== 0 || event.target.closest("button,input,a,label")) {
        return;
      }
      event.preventDefault();
      var dx = event.clientX - panel.offsetLeft;
      var dy = event.clientY - panel.offsetTop;
      function move(e) {
        panel.style.left = clampLeft(e.clientX - dx) + "px";
        panel.style.top = clampTop(e.clientY - dy) + "px";
      }
      function up() {
        document.removeEventListener("pointermove", move);
        document.removeEventListener("pointerup", up);
        remember(panel, key);
      }
      document.addEventListener("pointermove", move);
      document.addEventListener("pointerup", up);
    });
  }

  var panels = [];

  // A positioned panel with a header (drag handle) and a scrollable body.
  function makePanel(key, title, defaults, onClose) {
    var panel = element("div", "wl-panel");
    var head = element("div", "wl-head");
    head.appendChild(element("span", "wl-title", title));
    panel.appendChild(head);
    var body = element("div", "wl-body");
    panel.appendChild(body);
    if (onClose) {
      var close = element("button", "wl-close", "×");
      close.type = "button";
      close.title = "Close";
      close.addEventListener("click", onClose);
      head.appendChild(close);
    }
    place(panel, key, defaults);
    draggable(panel, head, key);
    panel.addEventListener("pointerdown", function () {
      bringToFront(panel);
    });
    panels.push({ panel: panel, key: key });
    document.body.appendChild(panel);
    return { panel: panel, head: head, body: body };
  }

  // The browser's resize handle sets the inline size; keep it once the pointer is released.
  function rememberResized() {
    panels.forEach(function (item) {
      var saved = layout[item.key] || {};
      if (isShown(item.panel) && (item.panel.offsetWidth !== saved.width ||
          (item.panel.style.height && item.panel.offsetHeight !== saved.height))) {
        remember(item.panel, item.key);
      }
    });
  }

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text) {
      node.textContent = text;
    }
    return node;
  }

  function section(parent, title) {
    var node = element("div", "wl-section");
    node.appendChild(element("h4", null, title));
    parent.appendChild(node);
    return node;
  }

  function button(text, onClick, secondary) {
    var b = element("button", secondary ? "wl-secondary" : null, text);
    b.type = "button";
    b.addEventListener("click", onClick);
    return b;
  }

  function checkbox(id, text, onChange) {
    var label = element("label", "wl-toggle");
    var box = document.createElement("input");
    box.type = "checkbox";
    box.id = id;
    box.addEventListener("change", function () {
      onChange(box.checked);
    });
    label.appendChild(box);
    label.appendChild(document.createTextNode(text));
    return { label: label, box: box };
  }

  // A drop-down list of [value, label] options.
  function select(id, options, value, onChange) {
    var node = document.createElement("select");
    node.id = id;
    options.forEach(function (entry) {
      var option = element("option", null, entry[1]);
      option.value = entry[0];
      node.appendChild(option);
    });
    node.value = value;
    node.addEventListener("change", function () { onChange(node.value); });
    return node;
  }

  // A slider with its current value shown (format(value) as text), calling onInput as it moves.
  function rangeSlider(id, min, max, step, value, format, onInput) {
    var input = document.createElement("input");
    input.type = "range";
    input.id = id;
    input.min = String(min);
    input.max = String(max);
    input.step = String(step);
    input.value = String(value);
    var shown = element("span", null, format(value));
    input.addEventListener("input", function () {
      var number = Number(input.value);
      shown.textContent = format(number);
      onInput(number);
    });
    return { input: input, value: shown };
  }

  // A labelled row of a settings table: label, control, and an optional unit or value text.
  function settingRow(table, label, control, unit) {
    var tr = element("tr");
    tr.appendChild(element("td", null, label));
    var cell = element("td", "wl-input");
    cell.appendChild(control);
    tr.appendChild(cell);
    var after = element("td", "wl-km");
    if (unit) {
      after.appendChild(typeof unit === "string" ? document.createTextNode(unit) : unit);
    }
    tr.appendChild(after);
    table.appendChild(tr);
    return tr;
  }

  function percent(value) {
    return Math.round(value * 100) + " %";
  }

  var REPLAY_SPEEDS = [["60", "1 min / s"], ["300", "5 min / s"], ["600", "10 min / s"], ["1800", "30 min / s"], ["3600", "1 h / s"]];

  function numberInput() {
    var input = document.createElement("input");
    input.type = "number";
    input.step = "1";
    input.value = 100;
    return input;
  }

  // Small panels opened from the central panel: key, toggle / panel title.
  var DEMOS = [
    ["river", "River"],
    ["flood", "Flood"],
    ["replay", "Data replay"],
    ["live", "Live replay"],
    ["terrain", "Terrain"],
    ["sun", "Sun settings"],
    ["fees", "Water-fee model"]
  ];
  // Collapsible categories of the central panel (key, title), and the one of each panel toggle.
  // Which are open is kept in localStorage; open by default unless said otherwise.
  var CATEGORIES = [["aoi", "Area of interest"], ["time", "Time"], ["rain", "Rain"], ["river", "River"],
    ["terrain", "Terrain & Buildings"], ["names", "Names"]];
  // The buildings controls are a collapsible section of the Terrain & Buildings category (see build).
  var DEMO_CATEGORY = {
    river: "river", flood: "river", replay: "time", live: "time", terrain: "terrain", sun: "time", fees: "river"
  };
  var CATEGORIES_KEY = "wl-categories";

  function loadOpenCategories() {
    try {
      var stored = JSON.parse(localStorage.getItem(CATEGORIES_KEY));
      if (stored && typeof stored === "object") {
        return stored;
      }
    } catch (e) { /* storage unavailable or invalid: all open */ }
    return {};
  }

  function category(parent, key, title, open, closedByDefault) {
    var node = element("div", "wl-category");
    var head = element("button", "wl-category-head", title);
    head.type = "button";
    head.id = "water-level-control-category-" + key;
    var body = element("div", "wl-category-body");
    function show(isOpen) {
      head.setAttribute("aria-expanded", isOpen ? "true" : "false");
      body.style.display = isOpen ? "" : "none";
    }
    show(Object.prototype.hasOwnProperty.call(open, key) ? open[key] !== false : !closedByDefault);
    head.addEventListener("click", function () {
      open[key] = body.style.display === "none";
      show(open[key]);
      try {
        localStorage.setItem(CATEGORIES_KEY, JSON.stringify(open));
      } catch (e) { /* storage unavailable */ }
    });
    node.appendChild(head);
    node.appendChild(body);
    parent.appendChild(node);
    return body;
  }

  // ---------------------------------------------------------------------------
  // AOI change (Robin, 2026-11-25): the area-of-interest selector in the panel, replacing the
  // WPS "AOI selector" of Explore data. It uses the AOI module's REST routes:
  //   POST /aoi/reach {km_start, km_end} builds (or reuses) an Alzette reach and makes it current;
  //   GET /aoi/reaches lists the reaches already built, to switch back to one at once.
  // Once a reach is current, the status banner (refreshAoiStatus) shows what is computing, and
  // the map reloads its layers for the new reach.
  // ---------------------------------------------------------------------------
  function buildAoiControls(parent) {
    ui.aoiCurrent = element("div", "wl-note", "Current area: …");
    parent.appendChild(ui.aoiCurrent);

    var note = element("div", "wl-note");

    function selectReach(river, kmStart, kmEnd) {
      note.textContent = "Preparing " + river + " km " + kmStart + "–" + kmEnd + "…";
      return fetch(BACKEND_URL + "/aoi/reach", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ river: river, km_start: Number(kmStart), km_end: Number(kmEnd) })
      })
        .then(function (response) { return response.json(); })
        .then(function (data) {
          if (data.status !== "processing") {
            note.textContent = data.message || "The area could not be selected.";
            return;
          }
          return waitForTask(data.task_id).then(function (task) {
            if (task.status !== "finished") {
              note.textContent = "The area could not be built: " + (task.message || "unknown error");
              return;
            }
            note.textContent = "Selected. The results are recomputed for it (see the banner at the top).";
            loadReaches();
            refreshAoiStatus();  // switch the map to the new reach now, not at the next 15 s poll
          });
        })
        .catch(function () { note.textContent = "Could not reach the backend."; });
    }

    // Areas already built: switching to one reuses it (no new build).
    var built = document.createElement("select");
    built.id = "water-level-control-aoi-built";
    built.style.width = "100%";
    function loadReaches() {
      return fetch(BACKEND_URL + "/aoi/reaches")
        .then(function (response) { return response.json(); })
        .then(function (data) {
          built.textContent = "";
          (data.reaches || []).filter(function (r) { return r.status === "ready"; })
            .sort(function (a, b) { return a.river.localeCompare(b.river) || a.km_start - b.km_start || a.km_end - b.km_end; })
            .forEach(function (r) {
              var option = element("option", null, r.river + " km " + r.km_start + "–" + r.km_end);
              option.value = JSON.stringify([r.river, r.km_start, r.km_end]);
              option.selected = r.reach_id === aoiReach;
              built.appendChild(option);
            });
        })
        .catch(function () { /* backend unreachable: keep the list */ });
    }
    var builtLabel = element("label", "wl-field");
    builtLabel.appendChild(element("span", null, "Built areas"));
    builtLabel.appendChild(built);
    parent.appendChild(builtLabel);
    parent.appendChild(button("Switch to this area", function () {
      if (!built.value) {
        return;
      }
      var choice = JSON.parse(built.value);
      selectReach(choice[0], choice[1], choice[2]);
    }));

    // A new area: a kilometre range of the Alzette, validated by the backend before anything is
    // built. The river is fixed to the Alzette (2026-11-26); its first and last marker come
    // from GET /aoi/river.json and limit the km fields.
    var riverNote = element("div", "wl-note", "New area: Alzette, river kilometres (one decimal):");
    parent.appendChild(riverNote);
    var kmFrom = document.createElement("input");
    kmFrom.type = "number";
    kmFrom.id = "water-level-control-aoi-from";
    kmFrom.step = "0.1";
    kmFrom.min = "0";
    var kmTo = document.createElement("input");
    kmTo.type = "number";
    kmTo.id = "water-level-control-aoi-to";
    kmTo.step = "0.1";
    kmTo.min = "0";
    fetch(BACKEND_URL + "/aoi/river.json")
      .then(function (response) { return response.json(); })
      .then(function (info) {
        if (info.km_start === null || info.km_end === null) {
          return;
        }
        [kmFrom, kmTo].forEach(function (input) {
          input.min = String(info.km_start);
          input.max = String(info.km_end);
        });
        riverNote.textContent = "New area: " + info.river + " km " + info.km_start + "–" + info.km_end +
          " (one decimal, a marker every 100 m):";
      })
      .catch(function () { /* backend unreachable: the backend still checks the range */ });
    [["From km", kmFrom], ["To km", kmTo]].forEach(function (entry) {
      var field = element("label", "wl-field");
      field.appendChild(element("span", null, entry[0]));
      field.appendChild(entry[1]);
      parent.appendChild(field);
    });
    parent.appendChild(button("Build and select", function () {
      if (kmFrom.value === "" || kmTo.value === "") {
        note.textContent = "Enter both kilometres.";
        return;
      }
      selectReach("Alzette", kmFrom.value, kmTo.value);
    }));
    parent.appendChild(note);

    // The current area's layers (they follow every switch).
    var centerline = checkbox("water-level-control-aoi-centerline", "River centerline", function (checked) {
      toggleMapLayer("aoi-centerline", checked);
    });
    var corridor = checkbox("water-level-control-aoi-corridor", "Area outline (500 m corridor)", function (checked) {
      toggleMapLayer("aoi-corridor", checked);
    });
    parent.appendChild(centerline.label);
    parent.appendChild(corridor.label);

    ui.loadAoiReaches = loadReaches;
    loadReaches();
  }

  var CENTRAL_WIDTH = 220;
  var SMALL_WIDTH = 290;

  function build() {
    var style = document.createElement("style");
    style.textContent = STYLE;
    document.head.appendChild(style);
    loadLayout();

    // Central panel: title, what the water shows, one toggle per demonstration.
    var centralLeft = Math.max(8, window.innerWidth - 64 - CENTRAL_WIDTH);
    var central = makePanel("central", "Alzette twin", { left: centralLeft, top: 72, width: CENTRAL_WIDTH });
    central.panel.id = "water-level-control";
    ui.mode = element("span", "wl-mode");
    central.head.appendChild(ui.mode);

    var demos = {};
    var openCategories = loadOpenCategories();
    var categories = {};
    CATEGORIES.forEach(function (entry) {
      categories[entry[0]] = category(central.body, entry[0], entry[1], openCategories);
    });
    buildAoiControls(categories.aoi);
    DEMOS.forEach(function (demo, index) {
      var key = demo[0];
      var small;
      // Opening or closing a panel only shows or hides it: what it controls is left as it is.
      function show(shown) {
        small.panel.style.display = shown ? "" : "none";
        toggle.box.checked = shown;
        if (shown) {
          bringToFront(small.panel);
          // Back on screen if the window got smaller while it was closed.
          small.panel.style.left = clampLeft(small.panel.offsetLeft) + "px";
          small.panel.style.top = clampTop(small.panel.offsetTop) + "px";
        }
      }
      var toggle = checkbox("water-level-control-show-" + key, demo[1], show);
      categories[DEMO_CATEGORY[key]].appendChild(toggle.label);
      small = makePanel(key, demo[1], {
        left: Math.max(8, centralLeft - SMALL_WIDTH - 12 - index * 24),
        top: 72 + index * 36,
        width: SMALL_WIDTH
      }, function () {
        // Closing the panel unticks its toggle.
        remember(small.panel, key);
        show(false);
      });
      small.panel.id = "water-level-control-" + key;
      small.panel.style.display = "none";
      demos[key] = small.body;
    });

    // Show sun: colour filter from the sun at the time shown. Off by default.
    var sun = checkbox("water-level-control-sun-toggle", "Show sun", function (checked) {
      showSun = checked;
      updateSun();
    });
    categories.time.appendChild(sun.label);

    // REM view: height above the river as colour, with contours. Off by default; style in the Terrain panel.
    var remToggle = checkbox("water-level-control-rem", "REM view", function (checked) {
      toggleNames("rem", checked);
    });
    categories.terrain.appendChild(remToggle.label);

    // Terrain: grayscale orthophoto and the shading of steep terrain. Changes apply at once and are kept.
    var look = section(demos.terrain, "Orthophoto");
    var grayscale = checkbox("water-level-control-grayscale", "Grayscale", function (checked) {
      terrainSettings.grayscale = checked;
      saveTerrainSettings();
      updateWater();
    });
    grayscale.box.checked = terrainSettings.grayscale;
    look.appendChild(grayscale.label);

    var shading = section(demos.terrain, "Slope shading");
    var slope = checkbox("water-level-control-slope", "Shade steep slopes", function (checked) {
      terrainSettings.slope = checked;
      saveTerrainSettings();
      toggleNames("slope", checked);
    });
    slope.box.checked = showSlope;
    shading.appendChild(slope.label);
    var shadingTable = element("table");
    function shadingRow(label, input, unit) {
      var tr = element("tr");
      tr.appendChild(element("td", null, label));
      var cell = element("td", "wl-input");
      cell.appendChild(input);
      tr.appendChild(cell);
      tr.appendChild(element("td", "wl-km", unit || ""));
      shadingTable.appendChild(tr);
    }
    function settingInput(type, key, attributes, parse) {
      var input = document.createElement("input");
      input.type = type;
      Object.keys(attributes).forEach(function (name) { input[name] = attributes[name]; });
      input.value = terrainSettings[key];
      input.addEventListener("input", function () {
        var value = parse(input.value);
        if (value === null) {
          return;
        }
        terrainSettings[key] = value;
        saveTerrainSettings();
        updateWater();
      });
      return input;
    }
    function angle(text) {
      var value = Number(text);
      return text !== "" && isFinite(value) && value >= 0 && value <= 90 ? value : null;
    }
    shadingRow("Starts at", settingInput("number", "start", { min: 0, max: 89, step: 1, id: "water-level-control-slope-start" }, angle), "\u00b0");
    shadingRow("Full colour at", settingInput("number", "full", { min: 1, max: 90, step: 1, id: "water-level-control-slope-full" }, angle), "\u00b0");
    shadingRow("Colour", settingInput("color", "colour", { id: "water-level-control-slope-colour" }, function (text) {
      return /^#[0-9a-fA-F]{6}$/.test(text) ? text : null;
    }));
    var opacityValue = element("span", null, Math.round(terrainSettings.opacity * 100) + " %");
    var opacity = settingInput("range", "opacity", { min: 0, max: 1, step: 0.05, id: "water-level-control-slope-opacity" }, function (text) {
      var value = Number(text);
      opacityValue.textContent = Math.round(value * 100) + " %";
      return value;
    });
    shadingRow("Opacity", opacity);
    shadingTable.lastChild.lastChild.appendChild(opacityValue);
    shading.appendChild(shadingTable);
    shading.appendChild(element("div", "wl-note",
      "Slopes steeper than the start angle get darker up to the full-colour angle; streets and gentle slopes stay clear."));

    // REM view: contour interval and opacity (shown with the REM view toggle of the Terrain & Buildings category).
    var remSection = section(demos.terrain, "REM view");
    var remTable = element("table");
    var intervalSelect = document.createElement("select");
    intervalSelect.id = "water-level-control-rem-interval";
    REM_INTERVALS.forEach(function (value) {
      var option = element("option", null, value + " m");
      option.value = String(value);
      intervalSelect.appendChild(option);
    });
    intervalSelect.value = String(REM_INTERVALS.indexOf(terrainSettings.remInterval) >= 0 ? terrainSettings.remInterval : TERRAIN_DEFAULTS.remInterval);
    intervalSelect.addEventListener("change", function () {
      terrainSettings.remInterval = Number(intervalSelect.value);
      saveTerrainSettings();
      updateWater();
    });
    var remOpacityValue = element("span", null, Math.round(terrainSettings.remOpacity * 100) + " %");
    var remOpacity = settingInput("range", "remOpacity", { min: 0, max: 1, step: 0.05, id: "water-level-control-rem-opacity" }, function (text) {
      var value = Number(text);
      remOpacityValue.textContent = Math.round(value * 100) + " %";
      return value;
    });
    [["Contour every", intervalSelect], ["Opacity", remOpacity]].forEach(function (row) {
      var tr = element("tr");
      tr.appendChild(element("td", null, row[0]));
      var cell = element("td", "wl-input");
      cell.appendChild(row[1]);
      tr.appendChild(cell);
      tr.appendChild(element("td", "wl-km"));
      remTable.appendChild(tr);
    });
    remTable.lastChild.lastChild.appendChild(remOpacityValue);
    remSection.appendChild(remTable);
    var ramp = element("div", "wl-ramp");
    ramp.style.background = "linear-gradient(to right, rgb(20,90,200), rgb(250,230,120))";
    remSection.appendChild(ramp);
    var rampLegend = element("div", "wl-legend");
    rampLegend.style.justifyContent = "space-between";
    rampLegend.appendChild(element("span", null, "at the river surface"));
    rampLegend.appendChild(element("span", null, "20 m above"));
    remSection.appendChild(rampLegend);
    remSection.appendChild(element("div", "wl-note",
      "Height of the ground above the river's own surface in the LiDAR DEM (Relative Elevation Model). Tick REM view in Terrain & Buildings to show it."));

    // Place names, shown as small 3D points (full name on hover). Both off by default.
    var names = categories.names;
    [["streets", "Street names"], ["other", "Other names"]].forEach(function (entry) {
      var toggle = checkbox("water-level-control-names-" + entry[0], entry[1], function (checked) {
        toggleNames(entry[0], checked);
      });
      layerBoxes[entry[0]] = toggle.box;
      names.appendChild(toggle.label);
    });

    // River: gauge stages, slider and manual level.
    var gauges = section(demos.river, "Gauge stage (cm above gauge zero)");
    var table = element("table");
    ui.pf = numberInput();
    ui.he = numberInput();
    ui.li = numberInput();
    [["Pfaffenthal", "km 35.0", ui.pf, "pf"], ["Hesperange", "km 45.4", ui.he, "he"], ["Livange", "km 54.3", ui.li, "li"]].forEach(function (row) {
      var tr = element("tr");
      var nameCell = element("td", null, row[0]);
      // Fly to the gauge.
      var go = button("\u2316", function () { flyToGauge(row[0]); }, true);
      go.className = "wl-go";
      go.title = "Fly to the " + row[0] + " gauge";
      nameCell.appendChild(go);
      // Use the gauge in the water level, or leave it out.
      var use = document.createElement("input");
      use.type = "checkbox";
      use.className = "wl-gauge-use";
      use.id = "water-level-control-gauge-" + row[3];
      use.checked = gaugeEnabled[row[3]];
      use.title = "Use the " + row[0] + " gauge in the water level (untick to leave it out)";
      use.addEventListener("change", function () { setGaugeEnabled(row[3], use.checked, use); });
      nameCell.appendChild(use);
      tr.appendChild(nameCell);
      tr.appendChild(element("td", "wl-km", row[1]));
      var cell = element("td", "wl-input");
      cell.appendChild(row[2]);
      tr.appendChild(cell);
      table.appendChild(tr);
    });
    gauges.appendChild(table);
    ui.gaugeNote = element("div", "wl-note");
    ui.gaugeNote.id = "water-level-control-gauge-note";
    gauges.appendChild(ui.gaugeNote);

    var slider = document.createElement("input");
    slider.type = "range";
    slider.min = "0";
    slider.max = "600";
    slider.step = "10";
    slider.value = "100";
    slider.title = "Same stage at every gauge";
    slider.addEventListener("input", function () {
      ui.pf.value = slider.value;
      ui.he.value = slider.value;
      ui.li.value = slider.value;
    });
    slider.addEventListener("change", function () {
      live = false;
      applyLevel(Number(slider.value), Number(slider.value), Number(slider.value));
    });
    gauges.appendChild(slider);

    var actions = element("div", "wl-row");
    actions.appendChild(button("Show level", function () {
      live = false;
      applyLevel(Number(ui.pf.value), Number(ui.he.value), ui.li.value === "" ? null : Number(ui.li.value));
    }));
    gauges.appendChild(actions);

    // Flow direction arrows on the water. On by default.
    var flow = section(demos.river, "Flow");
    var arrows = checkbox("water-level-control-arrows", "Flow direction arrows", function (checked) {
      showArrows = checked;
    });
    arrows.box.checked = showArrows;
    flow.appendChild(arrows.label);
    flow.appendChild(element("div", "wl-note",
      "Hover the water for its estimated velocity (Manning's equation, an estimate)."));

    // Look of the animated water; applied at once and kept.
    var look3d = section(demos.river, "Water appearance");
    var lookTable = element("table");
    [
      ["rippleSize", "Ripple size", 2, 20, 0.5, function (v) { return v + " m"; }],
      ["period", "Ripple period", 1, 10, 0.5, function (v) { return v + " s"; }],
      ["amplitude", "Wave strength", 0, 2, 0.05, function (v) { return v.toFixed(2); }],
      ["fps", "Animation", 5, 60, 5, function (v) { return v + " fps"; }]
    ].forEach(function (entry) {
      var control = rangeSlider("water-level-control-water-" + entry[0], entry[2], entry[3], entry[4], waterSettings[entry[0]],
        entry[5], function (value) {
          waterSettings[entry[0]] = value;
          saveSettings(WATER_KEY, waterSettings);
        });
      settingRow(lookTable, entry[1], control.input, control.value);
    });
    look3d.appendChild(lookTable);
    look3d.appendChild(button("Reset look", function () {
      ["rippleSize", "period", "amplitude", "fps"].forEach(function (key) {
        waterSettings[key] = WATER_DEFAULTS[key];
        var input = document.getElementById("water-level-control-water-" + key);
        input.value = String(WATER_DEFAULTS[key]);
        input.dispatchEvent(new Event("input"));
      });
    }, true));

    // The water surface itself, shown or hidden without reloading (the level keeps following the data).
    var waterToggle = checkbox("water-level-control-water-surface", "Water surface", function (checked) {
      setLayer("river", checked);
    });
    waterToggle.box.checked = showWater;
    layerBoxes.river = waterToggle.box;
    categories.river.insertBefore(waterToggle.label, categories.river.firstChild);

    // The water-fee model's result on the map, next to the model's own panel toggle (River category).
    var feesLayer = checkbox("water-level-control-layer-fees", "Water fees (model result)", function (checked) {
      toggleMapLayer("fees", checked);
    });
    ui.feesLayer = feesLayer.box;
    categories.river.appendChild(feesLayer.label);

    // Rain: the rainfall layer, what it shows, and the preparation of a period's rainfall.
    var rain = checkbox("water-level-control-layer-rain", "Show rain", function (checked) {
      toggleMapLayer("rain", checked);
    });
    rain.box.checked = mapLayers.rain;
    layerBoxes.rain = rain.box;
    categories.rain.appendChild(rain.label);
    ui.rainInfo = element("div", "wl-note");
    ui.rainInfo.id = "water-level-control-rain-info";
    categories.rain.appendChild(ui.rainInfo);
    categories.rain.appendChild(element("div", "wl-note",
      "Latest interpolated rainfall, or the replayed day's (prepared automatically for a replayed day)."));

    // Sun settings: dawn / dusk length and the strength of each tint of the Show sun filter.
    var sunSection = section(demos.sun, "Show sun filter");
    var sunTable = element("table");
    var sunTwilight = rangeSlider("water-level-control-sun-twilight", 10, 120, 5, sunSettings.twilightMin,
      function (v) { return v + " min"; }, function (value) {
        sunSettings.twilightMin = value;
        applySunSettings();
        saveSettings(SUN_KEY, sunSettings);
        updateSun();
      });
    settingRow(sunTable, "Dawn / dusk", sunTwilight.input, sunTwilight.value);
    [["day", "Day tint"], ["twilight", "Dawn tint"], ["night", "Night tint"]].forEach(function (entry) {
      var control = rangeSlider("water-level-control-sun-" + entry[0], 0, 1, 0.02, sunSettings[entry[0]], percent, function (value) {
        sunSettings[entry[0]] = value;
        applySunSettings();
        saveSettings(SUN_KEY, sunSettings);
        updateSun();
      });
      settingRow(sunTable, entry[1], control.input, control.value);
    });
    sunSection.appendChild(sunTable);
    sunSection.appendChild(element("div", "wl-note",
      "Tick Show sun in the Time category to see the filter; it follows the time shown (Luxembourg)."));

    // Water-fee model (eddie_building_population): population per building and the extra water pool
    // shared among the non-residential buildings by footprint area.
    var fees = section(demos.fees, "Parameters");
    var feesTable = element("table");
    var feeVolume = document.createElement("input");
    feeVolume.type = "number";
    feeVolume.id = "water-level-control-fees-volume";
    feeVolume.min = "0";
    feeVolume.max = "1000000000";
    feeVolume.step = "1000";
    feeVolume.value = "100000";
    settingRow(feesTable, "Water volume", feeVolume, "m³");
    var feeShare = rangeSlider("water-level-control-fees-percentage", 0, 1, 0.01, 0.1, percent, function () {});
    settingRow(feesTable, "Extra share", feeShare.input, feeShare.value);
    var feePrice = document.createElement("input");
    feePrice.type = "number";
    feePrice.id = "water-level-control-fees-price";
    feePrice.min = "0";
    feePrice.max = "1000";
    feePrice.step = "0.5";
    feePrice.value = "7";
    settingRow(feesTable, "Unit price", feePrice, "€/m³");
    fees.appendChild(feesTable);
    var feesNote = element("div", "wl-note");
    feesNote.id = "water-level-control-fees-note";
    function showFeesSummary() {
      return fetch(BACKEND_URL + "/building-population/summary.json")
        .then(function (response) { return response.json(); })
        .then(function (data) {
          if (data.status !== "success") {
            feesNote.textContent = data.message || "No result yet.";
            return;
          }
          var euro = new Intl.NumberFormat("en-IE", { maximumFractionDigits: 0 });
          feesNote.textContent = "Last result: " + euro.format(data.buildings) + " buildings, about " +
            euro.format(data.est_population) + " residents; " + euro.format(data.fee_buildings) +
            " non-residential buildings share " + euro.format(data.allocated_water_volume) + " m³ for €" +
            euro.format(data.total_cost) + " (pool " + euro.format(data.total_water_volume) + " m³ × " +
            percent(data.extra_water_pool / data.total_water_volume || 0) + ", €" + data.unit_price + "/m³).";
        })
        .catch(function () { feesNote.textContent = "Could not reach the backend."; });
    }
    fees.appendChild(button("Run model", function () {
      feesNote.textContent = "Running the model...";
      fetch(BACKEND_URL + "/building-population/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          total_water_volume: Number(feeVolume.value),
          percentage: Number(feeShare.input.value),
          unit_price: Number(feePrice.value)
        })
      })
        .then(function (response) { return response.json(); })
        .then(function (data) {
          if (data.status !== "processing") {
            feesNote.textContent = data.message || "The model could not be started.";
            return;
          }
          return waitForTask(data.task_id).then(function (task) {
            if (task.status !== "finished") {
              feesNote.textContent = "The model failed: " + (task.message || "unknown error");
              return;
            }
            return Promise.all([showFeesSummary(), refreshLayerStatus()]).then(function () {
              if (mapLayers.fees) {
                reloadView();
              }
            });
          });
        })
        .catch(function () { feesNote.textContent = "Could not reach the backend."; });
    }));
    fees.appendChild(feesNote);
    fees.appendChild(element("div", "wl-note",
      "Tick Water fees in the River category to show the result on the map. The run takes about a minute."));
    showFeesSummary();

    // Scenario 1 (15-minute gauge readings) and scenario 2 (seasonal rain), from the
    // eddie_water_fees plugin. It writes columns on building_population, so the result is
    // drawn by the same "Water fees (model result)" layer: tick it under River.
    var fees15 = section(demos.fees, "All five scenarios (one run)");
    var fees15Table = element("table");

    function fees15Text(id, placeholder) {
      var input = document.createElement("input");
      input.type = "text";
      input.id = id;
      input.placeholder = placeholder;
      return input;
    }

    // Scenario 3-5 parameters. Before this, only the backend defaults could be used
    // (150 EUR at 50 m, 2.5 EUR/m2, 160 l/person, 0.005 EUR/l, 80 % usable floor space).
    function fees15Number(id, value, min, max, step) {
      var input = document.createElement("input");
      input.type = "number";
      input.id = id;
      input.min = String(min);
      input.max = String(max);
      input.step = String(step);
      input.value = String(value);
      return input;
    }

    // One heading per scenario: each group of fields sits under the scenario it drives.
    function fees15Heading(text) {
      var tr = element("tr");
      var td = element("td", null, text);
      td.colSpan = 3;
      td.style.cssText = "padding:9px 0 3px;font-weight:600;font-size:.85em";
      tr.appendChild(td);
      fees15Table.appendChild(tr);
    }

    fees15Heading("Scenario 1 · river water tariff (the season)");
    var gaugeFrom = fees15Text("water-level-control-fees15-from", "not used: scenario 1 runs the season");
    settingRow(fees15Table, "Gauges from (UTC)", gaugeFrom, "");
    var gaugeTo = fees15Text("water-level-control-fees15-to", "not used: scenario 1 runs the season");
    settingRow(fees15Table, "Gauges to (UTC)", gaugeTo, "");

    var fee15Share = rangeSlider("water-level-control-fees15-percentage", 0, 1, 0.01, 0.04, percent, function () {});
    settingRow(fees15Table, "Extra share", fee15Share.input, fee15Share.value);

    var fee15Price = document.createElement("input");
    fee15Price.type = "number";
    fee15Price.min = "0";
    fee15Price.max = "1000";
    fee15Price.step = "0.5";
    fee15Price.value = "7";
    settingRow(fees15Table, "Tariff", fee15Price, "€/m³");

    fees15Heading("Scenario 2 · seasonal rain on the roofs");
    var rainSeason = document.createElement("select");
    ["winter", "spring", "summer", "autumn"].forEach(function (name) {
      rainSeason.appendChild(element("option", null, name));
    });
    rainSeason.value = "summer";
    settingRow(fees15Table, "Rain season", rainSeason, "");

    var rainYear = document.createElement("input");
    rainYear.type = "number";
    rainYear.min = "1950";
    rainYear.max = "2100";
    rainYear.step = "1";
    rainYear.value = "2024";
    settingRow(fees15Table, "Season year", rainYear, "");

    var rainPrice = document.createElement("input");
    rainPrice.type = "number";
    rainPrice.min = "0";
    rainPrice.max = "1000";
    rainPrice.step = "0.5";
    rainPrice.value = "4.5";
    settingRow(fees15Table, "Rain fee", rainPrice, "€/m³");

    fees15Heading("Scenario 3 · flood-risk proximity levy");
    var floodDistance = fees15Number("water-level-control-fees15-distance", 50, 0, 10000, 1);
    settingRow(fees15Table, "Flood zone", floodDistance, "m");
    var floodFee = fees15Number("water-level-control-fees15-flood-fee", 150, 0, 1000000, 5);
    settingRow(fees15Table, "Flood levy", floodFee, "€");

    fees15Heading("Scenario 4 · water-quality surcharge");
    var qualityPrice = fees15Number("water-level-control-fees15-quality-price", 2.5, 0, 1000, 0.1);
    settingRow(fees15Table, "Quality fee", qualityPrice, "€/m²");

    fees15Heading("Scenario 5 · water consumption");
    var litersPerson = fees15Number("water-level-control-fees15-liters", 160, 0, 10000, 10);
    settingRow(fees15Table, "Use per person", litersPerson, "l");
    var literPrice = fees15Number("water-level-control-fees15-liter-price", 0.005, 0, 100, 0.001);
    settingRow(fees15Table, "Use fee", literPrice, "€/l");
    var usableSpace = fees15Number("water-level-control-fees15-usable", 0.8, 0, 1, 0.05);
    settingRow(fees15Table, "Usable floor space", usableSpace, "0-1");

    var rainNormals = checkbox("water-level-control-fees15-normals", "Use 1991-2020 normals (offline)", function () {});
    fees15.appendChild(fees15Table);
    fees15.appendChild(rainNormals.label);

    var fees15Note = element("div", "wl-note");
    function showFees15Summary() {
      return fetch(BACKEND_URL + "/water-fees/summary.json")
        .then(function (response) { return response.json(); })
        .then(function (data) {
          if (data.status !== "success") {
            fees15Note.textContent = data.message || "No result yet.";
            return;
          }
          var euro = new Intl.NumberFormat("en-IE", { maximumFractionDigits: 0 });
          // AOI change (Robin, 2026-11-25): outside the gauge coverage (Alzette km 35-54.34)
          // scenario 1 is not computed, so tariff_mean_eur is null - show the reason instead
          // of "mean tariff €0". A partly covered reach keeps the tariff and adds the note.
          var tariffText = data.tariff_mean_eur === null || data.tariff_mean_eur === undefined
            ? "river tariff " + (data.tariff_note || "not computed")
            : "mean tariff €" + euro.format(data.tariff_mean_eur) + " over " + data.intervals +
              " intervals (" + data.gauge_quality + ")" + (data.tariff_note ? " [" + data.tariff_note + "]" : "");
          fees15Note.textContent = "Last run: " + euro.format(data.buildings) + " buildings (" +
            euro.format(data.fee_buildings) + " non-residential); " + tariffText + "; rain " +
            data.rain_season + " " + data.rain_mm + " mm -> " +
            euro.format(data.rain_volume_m3) + " m³ on the roofs, €" + euro.format(data.rainwater_cost_eur) + ".";
        })
        .catch(function () { fees15Note.textContent = "Could not reach the backend."; });
    }

    fees15.appendChild(button("Run the five scenarios", function () {
      fees15Note.textContent = "Running the model...";
      fetch(BACKEND_URL + "/water-fees/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          percentage: Number(fee15Share.input.value),
          unit_price: Number(fee15Price.value),
          rainwater_unit_price: Number(rainPrice.value),
          distance_threshold: Number(floodDistance.value),
          flood_risk_base_fee: Number(floodFee.value),
          quality_unit_price: Number(qualityPrice.value),
          liters_per_person: Number(litersPerson.value),
          unit_price_per_liter: Number(literPrice.value),
          usable_space_pct: Number(usableSpace.value),
          gauge_start: gaugeFrom.value || null,
          gauge_end: gaugeTo.value || null,
          season: rainSeason.value,
          season_year: Number(rainYear.value),
          use_normals: rainNormals.box.checked
        })
      })
        .then(function (response) { return response.json(); })
        .then(function (data) {
          if (data.status !== "processing") {
            fees15Note.textContent = data.message || "The model could not be started.";
            return;
          }
          return waitForTask(data.task_id).then(function (task) {
            if (task.status !== "finished") {
              fees15Note.textContent = "The model failed: " + (task.message || "unknown error");
              return;
            }
            return showFees15Summary().then(function () {
              if (mapLayers.fees) {
                reloadView();
              }
            });
          });
        })
        .catch(function () { fees15Note.textContent = "Could not reach the backend."; });
    }));

    fees15.appendChild(fees15Note);
    fees15.appendChild(element("div", "wl-note",
      "The fee is written on the buildings, then read back from PostGIS. About a minute " +
      "(the gauges come from heichwaasser.lu, the rain from the ERA5 archive)."));
    showFees15Summary();

    // Per-building prices on click: the plugin serves the popup script itself, so this feature
    // lives entirely in eddie_water_fees and no other panel or popup has to change.
    if (!document.getElementById("water-fees-popup-script")) {
      var feePopup = document.createElement("script");
      feePopup.id = "water-fees-popup-script";
      feePopup.src = BACKEND_URL + "/water-fees/popup.js";
      feePopup.async = true;
      document.body.appendChild(feePopup);
    }

    // Rainfall of a period: interpolated now (POST /load-precipitation-period), shown in later replays.
    var rainPeriod = category(categories.rain, "rain-period", "Prepare a period", openCategories, true);
    rainPeriod.appendChild(element("div", "wl-note",
      "Fetches the ASTA rain observations of the period, then interpolates them over the reach."));
    var rainStart = document.createElement("input");
    rainStart.type = "datetime-local";
    rainStart.id = "water-level-control-rain-start";
    var rainEnd = document.createElement("input");
    rainEnd.type = "datetime-local";
    rainEnd.id = "water-level-control-rain-end";
    [["From (UTC)", rainStart], ["To (UTC)", rainEnd]].forEach(function (entry) {
      var field = element("label", "wl-field");
      field.appendChild(element("span", null, entry[0]));
      field.appendChild(entry[1]);
      rainPeriod.appendChild(field);
    });
    var rainNote = element("div", "wl-note");
    rainNote.id = "water-level-control-rain-period-note";
    var rainActions = element("div", "wl-row");
    rainActions.appendChild(button("Interpolate rainfall", function () {
      if (!rainStart.value || !rainEnd.value || rainEnd.value < rainStart.value) {
        rainNote.textContent = "Choose a start and a later end.";
        return;
      }
      var startIso = rainStart.value + ":00Z", endIso = rainEnd.value + ":00Z";
      rainNote.textContent = "Fetching the rain observations (ASTA)...";
      fetchRainObservations(startIso, endIso)
        .then(function (task) {
          if (task.status !== "finished") {
            throw new Error("The observations could not be fetched: " + (task.message || "unknown error"));
          }
          rainNote.textContent = "Interpolating...";
          return fetch(BACKEND_URL + "/load-precipitation-period", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ start: startIso, end: endIso })
          });
        })
        .then(function (response) { return response.json(); })
        .then(function (data) {
          if (data.status !== "processing") {
            rainNote.textContent = data.message || data.status;
            return;
          }
          rainNote.textContent = "Interpolating " + (data.usable_timestamps - data.already_cached) + " time steps...";
          return waitForTask(data.task_id).then(function (task) {
            rainNote.textContent = task.status === "finished" ? "Rainfall ready: replay a day of the period to see it." :
              "Interpolation failed: " + (task.message || "unknown error");
          });
        })
        .catch(function (error) {
          rainNote.textContent = error && error.message && error.message.indexOf("observations") >= 0 ?
            error.message : "Could not reach the backend.";
        });
    }));
    rainPeriod.appendChild(rainActions);
    rainPeriod.appendChild(rainNote);

    // Live replay: back to the latest stored gauge readings.
    var livePanel = section(demos.live, "Latest gauge readings");
    var liveActions = element("div", "wl-row");
    liveActions.appendChild(button("Live level", goLive, true));
    livePanel.appendChild(liveActions);
    ui.liveInfo = element("div", "wl-note", "Loading live gauge data...");
    ui.liveInfo.id = "water-level-control-live";
    livePanel.appendChild(ui.liveInfo);
    // How often the latest readings are checked again.
    var pollTable = element("table");
    var pollSelect = select("water-level-control-poll", POLL_MINUTES.map(function (value) {
      return [String(value), value + " min"];
    }), String(waterSettings.pollMin), function (value) {
      waterSettings.pollMin = Number(value);
      saveSettings(WATER_KEY, waterSettings);
      scheduleLive();
    });
    settingRow(pollTable, "Check every", pollSelect);
    livePanel.appendChild(pollTable);

    // Flood panel (opened by the Flood toggle of the River category): scenarios in Floods, the official hazard maps in Zones.
    var scenarios = category(demos.flood, "floods", "Floods", openCategories);
    var hq10 = checkbox("water-level-control-hq10", "HQ10 – 10-year flood", function (checked) {
      setScenario(checked ? "hq10" : null);
    });
    var hq100 = checkbox("water-level-control-hq100", "HQ100 – 100-year flood", function (checked) {
      setScenario(checked ? "hq100" : null);
    });
    var hqextreme = checkbox("water-level-control-hqextreme", "HQextreme – extreme flood", function (checked) {
      setScenario(checked ? "hqextreme" : null);
    });
    ui.hq10 = hq10.box;
    ui.hq100 = hq100.box;
    ui.hqextreme = hqextreme.box;
    scenarios.appendChild(hq10.label);
    scenarios.appendChild(hq100.label);
    scenarios.appendChild(hqextreme.label);
    ui.report = element("a");
    ui.report.id = "water-level-control-report";
    ui.report.target = "_blank";
    ui.report.rel = "noopener";
    ui.report.style.display = "none";
    scenarios.appendChild(ui.report);
    ui.damage = element("div", "wl-note");
    ui.damage.id = "water-level-control-damage";
    scenarios.appendChild(ui.damage);
    var zones = category(demos.flood, "zones", "Zones", openCategories);
    [["hq10", "Flood zone HQ10"], ["hq100", "Flood zone HQ100"], ["hqextreme", "Flood zone HQextreme"]].forEach(function (entry) {
      var zone = checkbox("water-level-control-layer-zones-" + entry[0], entry[1], function (checked) {
        toggleMapLayer("zones-" + entry[0], checked);
      });
      if (entry[0] === "hq100") {
        layerBoxes.zones = zone.box;
      }
      zones.appendChild(zone.label);
    });
    zones.appendChild(element("div", "wl-note",
      "Official flood hazard maps (HWGK 2021): the area each flood would cover. Shown on top of the map."));

    // Data replay: one day of stored readings.
    var replay = section(demos.replay, "Replay a day");
    var row = element("div", "wl-row");
    var day = document.createElement("input");
    day.type = "date";
    row.appendChild(day);
    day.id = "water-level-control-replay-date";
    ui.replayDay = day;
    row.appendChild(day);
    row.appendChild(button("Replay day", function () {
      if (day.value) {
        replayDay(day.value);
      }
    }));
    replay.appendChild(row);
    ui.replayInfo = element("div", "wl-note");
    ui.replayInfo.id = "water-level-control-replay";
    replay.appendChild(ui.replayInfo);
    // Playback speed of the replay: simulated time per real second.
    var speedTable = element("table");
    var speedSelect = select("water-level-control-replay-speed", REPLAY_SPEEDS, String(waterSettings.replaySpeed), function (value) {
      waterSettings.replaySpeed = Number(value);
      saveSettings(WATER_KEY, waterSettings);
      var t = findTerria();
      if (replayDate && t && t.timelineClock) {
        t.timelineClock.multiplier = waterSettings.replaySpeed;
      }
    });
    settingRow(speedTable, "Speed", speedSelect);
    replay.appendChild(speedTable);

    // Buildings: a collapsible section of the Terrain & Buildings category.
    var layers = category(categories.terrain, "buildings", "Buildings", openCategories);
    var buildings = checkbox("water-level-control-buildings", "3D buildings", toggleBuildings);
    layerBoxes.buildings = buildings.box;
    layers.appendChild(buildings.label);
    var legend = element("div", "wl-legend");
    [["#d7d2c8", "dry"], ["#ff961e", "touched by water"], ["#dc2828", "fully flooded"]].forEach(function (entry) {
      var item = element("span");
      var swatch = element("span", "wl-swatch");
      swatch.style.background = entry[0];
      item.appendChild(swatch);
      item.appendChild(document.createTextNode(entry[1]));
      legend.appendChild(item);
    });
    layers.appendChild(legend);

    // Digital Twin date (date-selector.js: load a day's precipitation and water level): its box is
    // moved into a collapsible section of the Time category instead of floating on its own.
    // The node is moved as it is, so its controls and behaviour stay those of date-selector.js.
    var dateSection = category(categories.time, "date", "Digital Twin date", openCategories);
    (function adoptDateBox(tries) {
      var box = document.getElementById("dt-date-box");
      if (box) {
        dateSection.appendChild(box);
      } else if (tries > 0) {
        setTimeout(function () { adoptDateBox(tries - 1); }, 500);
      }
    })(20);

    document.addEventListener("pointerup", rememberResized);
    document.addEventListener("mouseup", rememberResized);
    setMode("LIVE");

    refreshLive();
    scheduleLive();
    refreshLayerStatus();
    setInterval(showReplayStages, 500);
    // AOI change: every 15 s, check for another current reach (AOI layers) and a new
    // water-fee result.
    setInterval(refreshLayerStatus, 15000);
    setInterval(updateWater, 200);
    setInterval(updateSun, 250);
    window.requestAnimationFrame(animateWater);
    setupHover();
    // Clock changes (play, pause, scrubbing in Terria's own timeline) reach the Casual mode too.
    setInterval(function () {
      window.dispatchEvent(new CustomEvent("alzette-twin-clock", { detail: clockState() }));
    }, 500);

    // The mode chosen on the entry screen (twin-mode.js), now and whenever it changes.
    window.addEventListener("alzette-twin-mode", function (event) {
      setUiMode(event.detail && event.detail.mode);
    });
    if (window.TwinMode && window.TwinMode.current()) {
      setUiMode(window.TwinMode.current());
    }
    window.dispatchEvent(new CustomEvent("alzette-twin-ready", { detail: twinState() }));
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", build);
  } else {
    build();
  }
})();
