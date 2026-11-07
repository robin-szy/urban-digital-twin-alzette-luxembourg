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
  // How often the latest gauge readings are checked again.
  var LIVE_POLL_MS = 5 * 60 * 1000;

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

  function loadTerrainSettings() {
    var settings = Object.assign({}, TERRAIN_DEFAULTS);
    try {
      var stored = JSON.parse(localStorage.getItem(TERRAIN_KEY));
      if (stored && typeof stored === "object") {
        Object.keys(TERRAIN_DEFAULTS).forEach(function (key) {
          if (typeof stored[key] === typeof TERRAIN_DEFAULTS[key]) {
            settings[key] = stored[key];
          }
        });
      }
    } catch (e) { /* storage unavailable or invalid: defaults */ }
    return settings;
  }

  function saveTerrainSettings() {
    try {
      localStorage.setItem(TERRAIN_KEY, JSON.stringify(terrainSettings));
    } catch (e) { /* storage unavailable */ }
  }

  var terrainSettings = loadTerrainSettings();

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
    return (showSlope ? "slope/" : "") + (showRem ? "rem/" : "") + (kinds.length ? "names/" + kinds.join(",") + "/" : "");
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

  function loadInit(path) {
    currentView = path;
    var prefix = showBuildings ? "buildings/" : "";
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
  }

  function toggleBuildings(checked) {
    showBuildings = checked;
    if (previous) {
      // Reload the current level without animating it again.
      loadInit("level/" + previous.pf + "/" + previous.he + "/" + previous.li);
    } else if (currentView) {
      loadInit(currentView);
    } else if (checked) {
      window.location.hash = BACKEND_URL + "/terria-water-init/" + namesPrefix() + "buildings.json";
    }
  }

  function toggleNames(kind, checked) {
    if (kind === "slope") {
      showSlope = checked;
    } else if (kind === "rem") {
      showRem = checked;
    } else {
      showNames[kind] = checked;
    }
    if (previous) {
      // Reload the current level without animating it again.
      loadInit("level/" + previous.pf + "/" + previous.he + "/" + previous.li);
    } else if (currentView) {
      loadInit(currentView);
    } else {
      window.location.hash = BACKEND_URL + "/terria-water-init/" + namesPrefix() +
        (showBuildings ? "buildings" : "names") + ".json";
    }
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

    var material = new Cesium.Material({
      fabric: {
        uniforms: { normalMap: WATER_NORMALS, rippleSize: 7.0, period: 3.0, amplitude: 0.8, time: 0.0 },
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
    water.itemId = item ? item.uniqueId : null;
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
    var visible = !!item && item.show !== false;
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
  // WATER_FPS frames per second so its ripples move.
  var WATER_FPS = 30;
  var lastWaterFrame = 0;

  function animateWater(now) {
    if (water.animate && water.shown && now - lastWaterFrame >= 1000 / WATER_FPS) {
      lastWaterFrame = now;
      water.shown.material.uniforms.time = (now / 1000) % 3600;
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
        if (!currentView &&
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
  }

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
    ".wl-panel input[type=number],.wl-panel input[type=date]{width:100%;box-sizing:border-box;",
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
    // The Digital Twin date box (date-selector.js) inside the Data category: no longer floating.
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

  function isShown(panel) {
    return panel.style.display !== "none";
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
    ["terrain", "Terrain"]
  ];
  // Collapsible categories of the central panel (key, title), and the one of each panel toggle.
  // Which are open is kept in localStorage; all open by default.
  var CATEGORIES = [["names", "Names"], ["river", "River"], ["terrain", "Terrain"], ["data", "Data"]];
  // The buildings controls are a collapsible section of the Terrain category (see build).
  var DEMO_CATEGORY = { river: "river", flood: "river", replay: "data", live: "data", terrain: "terrain" };
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

  function category(parent, key, title, open) {
    var node = element("div", "wl-category");
    var head = element("button", "wl-category-head", title);
    head.type = "button";
    head.id = "water-level-control-category-" + key;
    var body = element("div", "wl-category-body");
    function show(isOpen) {
      head.setAttribute("aria-expanded", isOpen ? "true" : "false");
      body.style.display = isOpen ? "" : "none";
    }
    show(open[key] !== false);
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
    DEMOS.forEach(function (demo, index) {
      var key = demo[0];
      var small;
      var toggle = checkbox("water-level-control-show-" + key, demo[1], function (checked) {
        small.panel.style.display = checked ? "" : "none";
        if (checked) {
          bringToFront(small.panel);
          // Back on screen if the window got smaller while it was closed.
          small.panel.style.left = clampLeft(small.panel.offsetLeft) + "px";
          small.panel.style.top = clampTop(small.panel.offsetTop) + "px";
        }
      });
      small = makePanel(key, demo[1], {
        left: Math.max(8, centralLeft - SMALL_WIDTH - 12 - index * 24),
        top: 72 + index * 36,
        width: SMALL_WIDTH
      }, function () {
        // Closing the panel unticks its toggle.
        remember(small.panel, key);
        small.panel.style.display = "none";
        toggle.box.checked = false;
      });
      small.panel.id = "water-level-control-" + key;
      small.panel.style.display = "none";
      categories[DEMO_CATEGORY[key]].appendChild(toggle.label);
      demos[key] = small.body;
    });

    // Show sun: colour filter from the sun at the time shown. Off by default.
    var sun = checkbox("water-level-control-sun-toggle", "Show sun", function (checked) {
      showSun = checked;
      updateSun();
    });
    categories.data.appendChild(sun.label);

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

    // REM view: contour interval and opacity (shown with the REM view toggle of the Terrain category).
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
      "Height of the ground above the river's own surface in the LiDAR DEM (Relative Elevation Model). Tick REM view in the Terrain category to show it."));

    // Place names, shown as small 3D points (full name on hover). Both off by default.
    var names = categories.names;
    [["streets", "Street names"], ["other", "Other names"]].forEach(function (entry) {
      var toggle = checkbox("water-level-control-names-" + entry[0], entry[1], function (checked) {
        toggleNames(entry[0], checked);
      });
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

    // Live replay: back to the latest stored gauge readings.
    var livePanel = section(demos.live, "Latest gauge readings");
    var liveActions = element("div", "wl-row");
    liveActions.appendChild(button("Live level", function () {
      live = true;
      liveLevel = null;
      refreshLive();
    }, true));
    livePanel.appendChild(liveActions);
    ui.liveInfo = element("div", "wl-note", "Loading live gauge data...");
    ui.liveInfo.id = "water-level-control-live";
    livePanel.appendChild(ui.liveInfo);

    // Flood scenarios.
    var scenarios = section(demos.flood, "Flood scenarios");
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

    // Data replay: one day of stored readings.
    var replay = section(demos.replay, "Replay a day");
    var row = element("div", "wl-row");
    var day = document.createElement("input");
    day.type = "date";
    row.appendChild(day);
    row.appendChild(button("Replay day", function () {
      if (!day.value) {
        return;
      }
      var selected = day.value;
      // Check the day first: a day without stored readings would send Terria to a water
      // surface that cannot load.
      fetch(BACKEND_URL + "/terria-water-init/date/" + selected + ".json")
        .then(function (response) {
          if (!response.ok) {
            return response.json().then(function (data) {
              ui.replayInfo.textContent = data.message || "No gauge readings stored for " + selected + ".";
            });
          }
          ui.replayInfo.textContent = "Replaying " + selected + ".";
          live = false;
          previous = null;
          setScenarioBoxes(null);
          scenario = null;
          updateReportLink();
          setMode("REPLAY");
          replayDate = selected;
          loadInit("date/" + selected + withoutSuffix());
          return fetch(BACKEND_URL + "/water-level/day/" + selected + ".json")
            .then(function (stagesResponse) { return stagesResponse.json(); })
            .then(function (data) {
              if (data.status === "success") {
                data.ms = data.times.map(function (time) { return Date.parse(time); });
                replayStages = data;
              }
            });
        })
        .catch(function () {
          ui.replayInfo.textContent = "Could not reach the backend.";
        });
    }));
    replay.appendChild(row);
    ui.replayInfo = element("div", "wl-note");
    ui.replayInfo.id = "water-level-control-replay";
    replay.appendChild(ui.replayInfo);

    // Buildings: a collapsible section of the Terrain category.
    var layers = category(categories.terrain, "buildings", "Buildings", openCategories);
    var buildings = checkbox("water-level-control-buildings", "3D buildings", toggleBuildings);
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
    // moved into a collapsible section of the Data category instead of floating on its own.
    // The node is moved as it is, so its controls and behaviour stay those of date-selector.js.
    var dateSection = category(categories.data, "date", "Digital Twin date", openCategories);
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
    setInterval(refreshLive, LIVE_POLL_MS);
    setInterval(showReplayStages, 500);
    setInterval(updateWater, 200);
    setInterval(updateSun, 250);
    window.requestAnimationFrame(animateWater);
    setupHover();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", build);
  } else {
    build();
  }
})();
