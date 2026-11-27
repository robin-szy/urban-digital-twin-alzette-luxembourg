/* Additive TerriaJS popup adapter. No map layers, water controls or data are replaced.
 * $BACKEND_URL is substituted by the existing frontend entrypoint.
 * The comparison switches affect feature information only; map controls remain exclusive.
 */
(function (root) {
  "use strict";
  var SCENARIOS = ["hq10", "hq100", "hqextreme"];
  function label(s) { return s === "hqextreme" ? "HQextreme" : s.toUpperCase(); }
  var PREFIX = "fc_popup_";
  var BODY = '<h3>Building Flood Damage</h3><p><strong>Provisional · 2010 EUR</strong></p>' +
    '<p><strong>Building ID:</strong> {{fc_popup_id}}<br>' +
    '<strong>Building category:</strong> {{fc_popup_category}}<br>' +
    '<strong>Building footprint area:</strong> {{fc_popup_area}}</p>' +
    SCENARIOS.map(function (s) {
      return '{{#fc_popup_' + s + '_show}}<h4>' + label(s) + '</h4>' +
        '<p><strong>Estimated flood damage: {{fc_popup_' + s + '_damage}}</strong><br>' +
        'Estimated flood depth (wet-sample mean): {{fc_popup_' + s + '_depth}}<br>' +
        'Flood exposure: {{fc_popup_' + s + '_exposure}}</p>{{/fc_popup_' + s + '_show}}';
    }).join("") +
    '{{#fc_popup_no_scenario}}<p>No HQ damage scenario selected.</p>{{/fc_popup_no_scenario}}' +
    '{{#fc_popup_comparing}}<p>Alternative flood scenarios; amounts are not additive.</p>{{/fc_popup_comparing}}' +
    '{{#fc_popup_warning}}<p><strong>Data quality:</strong> {{fc_popup_warning}}</p>{{/fc_popup_warning}}' +
    // Dynamic flood costs (eddie_flood_costs.dynamic): the twin's own water, not the HQ maps.
    '{{#fc_dyn_show}}<h3>Dynamic flood costs</h3>' +
    '{{#fc_dyn_stage}}<p><strong>Water level shown: {{fc_dyn_stage}} cm at the gauges</strong><br>' +
    'Model 1 (peak depth): {{fc_dyn_m1}}<br>' +
    'Model 2 (water standing 6 h): {{fc_dyn_m2_6}}<br>' +
    'Model 2 (water standing 24 h): {{fc_dyn_m2_24}}<br>' +
    'Model 2 (water standing 72 h): {{fc_dyn_m2_72}}</p>{{/fc_dyn_stage}}' +
    '{{#fc_dyn_time}}<p><strong>Replay at {{fc_dyn_time}} UTC</strong><br>' +
    'Model 1 (highest water so far): {{fc_dyn_e1}}<br>' +
    'Model 2 (+ hours under water): {{fc_dyn_e2}}<br>' +
    'Model 3 (+ disruption, placeholder unit costs): {{fc_dyn_e3}}</p>{{/fc_dyn_time}}' +
    '{{#fc_dyn_note}}<p>{{fc_dyn_note}}</p>{{/fc_dyn_note}}{{/fc_dyn_show}}';

  function number(value, positive) {
    return typeof value === "number" && Number.isFinite(value) && value >= 0 && (!positive || value > 0);
  }
  function escapeText(value) {
    return String(value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function format(value, unit, positive) {
    if (!number(value, positive)) return "N/A";
    return new Intl.NumberFormat("en-IE", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value === 0 ? 0 : value) + unit;
  }
  function money(cost) {
    var value = cost && cost.damage_eur2010, status = cost && cost.monetary_status;
    if (!number(value) || !((status === "calculated_zero" && value === 0) ||
        (status === "calculated_positive" && value > 0))) return "N/A";
    return "€" + format(value, "");
  }
  function properties(feature, time) {
    try {
      if (!feature || !feature.properties) return {};
      return (typeof feature.properties.getValue === "function" ? feature.properties.getValue(time) : feature.properties) || {};
    } catch (e) { return {}; }
  }
  function buildingId(feature, time) {
    var id = properties(feature, time).buildingid;
    return typeof id === "string" && id.length > 4 && id.indexOf("ACT_") === 0 && id.trim() === id ? id : null;
  }
  function nativePayload(id, values, allowWholeArea) {
    // Reuse an already loaded monetary layer. Never guess from entity.id, names,
    // spatial proximity, or a different picked feature's properties.
    if (values.buildingid !== id || values.damage_currency !== "EUR" || values.damage_price_year !== 2010 ||
        values.damage_estimates_provisional !== true || !SCENARIOS.slice(0, 2).every(function (s) {
      return Object.prototype.hasOwnProperty.call(values, "damage_" + s + "_status");
    })) return null;
    var accepted = values.accepted_provisional_classification === true;
    var result = { buildingid: id, found: true, category: accepted ? values.jrc_category : "Unknown",
      currency: "EUR", price_year: 2010, provisional: true, warnings: [],
      footprint_area_m2: allowWholeArea !== false && values.monetary_method && number(values.footprint_part_count, true) ? values.footprint_area_m2 : null };
    SCENARIOS.forEach(function (s) {
      if (!Object.prototype.hasOwnProperty.call(values, "damage_" + s + "_status")) return;
      result[s] = { damage_eur2010: accepted ? values["damage_" + s + "_eur2010"] : null,
        monetary_status: values["damage_" + s + "_status"],
        depth_mean_m: values["depth_" + s + "_mean_m"], exposure_status: values["exposure_" + s + "_status"],
        quality_flags: String(values["damage_" + s + "_quality_flags"] || "").split("|").filter(Boolean) };
    });
    return result;
  }
  function verifiedPayload(id, payload) {
    return payload && payload.buildingid === id && typeof payload.found === "boolean" &&
      payload.currency === "EUR" && payload.price_year === 2010 && SCENARIOS.slice(0, 2).every(function (s) { return payload[s]; });
  }
  function display(id, payload, enabled) {
    var matched = verifiedPayload(id, payload) && payload.found;
    var warnings = matched && Array.isArray(payload.warnings) ? payload.warnings.slice() : [];
    var category = matched && ["Residential", "Commercial", "Industrial"].indexOf(payload.category) >= 0 ? payload.category : "Unknown";
    var result = { fc_popup_ready: true, fc_popup_id: escapeText(id), fc_popup_category: category,
      fc_popup_area: matched ? format(payload.footprint_area_m2, " m²", true) : "N/A",
      fc_popup_no_scenario: !SCENARIOS.some(function (s) { return enabled[s]; }),
      fc_popup_comparing: SCENARIOS.filter(function (s) { return enabled[s]; }).length > 1 };
    if (!matched) warnings.push("No verified flood-cost record is available for this building.");
    var exposure = { none: "Not exposed", touched_or_partial: "Touched or partly exposed", fully_contained: "Fully within the flood extent" };
    SCENARIOS.forEach(function (s) {
      var cost = matched ? payload[s] : null;
      result[PREFIX + s + "_show"] = !!enabled[s];
      result[PREFIX + s + "_damage"] = money(cost);
      result[PREFIX + s + "_depth"] = cost ? format(cost.depth_mean_m, " m") : "N/A";
      result[PREFIX + s + "_exposure"] = cost ? exposure[cost.exposure_status] || "N/A" : "N/A";
      if (!enabled[s]) return;
      if (!cost) { if (matched) warnings.push(label(s) + " is unavailable in this dataset."); return; }
      if (cost.monetary_status === "not_assessed") warnings.push(label(s) + " was not assessed for this newly approved classification; damage remains unknown.");
      if ((cost.quality_flags || []).indexOf("positive_depth_only_in_excluded_boundary_samples") >= 0) warnings.push("Positive depths occur only on excluded boundary samples; the interior-area estimate is zero.");
      if ((cost.quality_flags || []).indexOf("user_approved_provisional_not_independent_use_verification") >= 0 ||
          (cost.quality_flags || []).indexOf("residential_landuse_proxy_not_building_use_survey") >= 0) warnings.push("Approved provisional category; building use has not been independently verified.");
      if (cost.monetary_status === "unknown_classification") warnings.push("Building category is unresolved.");
      if (cost.monetary_status === "excluded_outside_corridor") warnings.push("Building is outside the assessed classification corridor.");
      if (cost.monetary_status === "unknown_depth_coverage") warnings.push("Incomplete depth coverage; full-building damage is unknown.");
      if ((cost.quality_flags || []).indexOf("vertical_datum_unverified") >= 0) warnings.push("Terrain elevations and reconstructed depths are provisional.");
      if ((cost.quality_flags || []).indexOf("user_approved_medium_confidence_commercial_proxy") >= 0) warnings.push("Building category uses an approved medium-confidence proxy.");
    });
    result.fc_popup_warning = escapeText(Array.from(new Set(warnings)).join(" "));
    return result;
  }
  function setProperty(feature, name, value, time) {
    var bag = feature.properties, existing = bag[name];
    if (properties(feature, time)[name] === value) return;
    if (existing && typeof existing.setValue === "function") existing.setValue(value);
    else if (typeof bag.addProperty === "function") bag.addProperty(name, value);
    else bag[name] = value;
  }
  function ownerOf(terria, feature) {
    var items = (terria.workbench && terria.workbench.items) || [];
    if (items.indexOf(feature._catalogItem) >= 0) return feature._catalogItem;
    return items.find(function (item) {
      return (item.mapItems || []).some(function (source) {
        var entities = source.entities, entity = feature.cesiumEntity || feature;
        return entities && (entities === feature.entityCollection ||
          (typeof entities.contains === "function" && entities.contains(entity)) ||
          (entities.values || []).indexOf(entity) >= 0);
      });
    });
  }
  function mapScenario(document, terria) {
    if (root.AlzetteTwin && typeof root.AlzetteTwin.getState === "function") {
      var state = root.AlzetteTwin.getState();
      if (state && Object.prototype.hasOwnProperty.call(state, "scenario")) return SCENARIOS.indexOf(state.scenario) >= 0 ? state.scenario : null;
    }
    var boxes = SCENARIOS.map(function (s) { return document && document.getElementById("water-level-control-" + s); });
    if (boxes.some(Boolean)) {
      return SCENARIOS.find(function (s, i) { return boxes[i] && boxes[i].checked; }) || null;
    }
    // Supports a scenario-specific catalog when the custom map controls are absent.
    var selected = null;
    ((terria.workbench && terria.workbench.items) || []).forEach(function (item) {
      if (item.type !== "czml") return;
      var url = String(item.url || ""), values = url.match(/(?:scenario|pfaffenthal_cm)=([^&]+)/);
      if (values) {
        var last = decodeURIComponent(values[1]).split(",").pop();
        if (SCENARIOS.indexOf(last) >= 0) selected = last;
      }
    });
    return selected;
  }
  // ---------------------------------------------------------------- dynamic flood costs
  // Which water the twin shows, read from Terria's init URL (water-level-control.js writes it):
  // ".../level/<pf>/<he>/<li>.json" (each a comma list ending with the level shown), a
  // scenario ("hq10"), or a replayed day (".../date/...").
  function shownWater(hash) {
    var text = decodeURIComponent(String(hash || ""));
    var at = text.indexOf("/level/");
    if (at < 0) return { kind: text.indexOf("/date/") >= 0 ? "date" : null };
    var parts = text.slice(at + 7).replace(/\.json.*$/, "").split("/");
    if (parts.length !== 3) return { kind: null };
    var stages = parts.map(function (part) { return part.split(",").pop(); });
    if (stages.some(function (v) { return SCENARIOS.indexOf(v) >= 0; })) return { kind: "scenario" };
    var numbers = stages.filter(function (v) { return v !== "x" && v !== ""; }).map(Number);
    if (!numbers.length || numbers.some(function (v) { return !Number.isFinite(v); })) return { kind: null };
    var same = Math.max.apply(null, numbers) - Math.min.apply(null, numbers) <= 1;
    return { kind: "level", stage: same ? Math.round(numbers[0] / 10) * 10 : null };
  }
  function euros(value) {
    return typeof value === "number" && Number.isFinite(value) ?
      "€" + new Intl.NumberFormat("en-IE", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value) : "N/A";
  }
  function isoMinute(date) {
    return date.toISOString().slice(0, 16) + ":00Z";
  }
  // Small line chart as inline SVG, built with DOM calls (not through Terria's HTML template).
  // series: [{name, color, points: [[x, y], ...]}]; refs: [{label, y}] dashed reference lines;
  // marker: x of the current slider level / replay time.
  var CHART_COLORS = { model1: "#7fb3ff", model2: "#ffb347", model3: "#ff7b7b", ref: "#b8bec8", text: "#d9dee6" };
  function compactEuro(value) {
    var a = Math.abs(value);
    if (a >= 1e6) return "€" + (value / 1e6).toFixed(a >= 1e7 ? 0 : 1) + "M";
    if (a >= 1e3) return "€" + (value / 1e3).toFixed(a >= 1e4 ? 0 : 1) + "k";
    return "€" + Math.round(value);
  }
  function lineChart(document, opts) {
    var NS = "http://www.w3.org/2000/svg", W = 320, H = 168, L = 46, R = 8, T = 36, B = 24;
    function el(name, attrs, text) {
      var node = document.createElementNS(NS, name);
      Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
      if (text !== undefined) node.textContent = text;
      return node;
    }
    var xs = [], ys = [0];
    opts.series.forEach(function (s) { s.points.forEach(function (p) { xs.push(p[0]); if (Number.isFinite(p[1])) ys.push(p[1]); }); });
    (opts.refs || []).forEach(function (r) { ys.push(r.y); });
    if (!xs.length) return null;
    var x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs), y1 = Math.max.apply(null, ys) || 1;
    if (x1 === x0) x1 = x0 + 1;
    function px(x) { return L + (x - x0) / (x1 - x0) * (W - L - R); }
    function py(y) { return H - B - y / y1 * (H - T - B); }
    var svg = el("svg", { width: W, height: H, viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": opts.title });
    svg.style.cssText = "display:block;margin:4px 0 8px;font:10px sans-serif;max-width:100%";
    svg.appendChild(el("title", {}, opts.title));
    svg.appendChild(el("text", { x: L, y: 11, fill: CHART_COLORS.text, "font-weight": "bold" }, opts.title));
    [0, 0.5, 1].forEach(function (f) {
      var y = py(y1 * f);
      svg.appendChild(el("line", { x1: L, x2: W - R, y1: y, y2: y, stroke: "#5a6270", "stroke-width": 0.5 }));
      svg.appendChild(el("text", { x: L - 4, y: y + 3, fill: CHART_COLORS.text, "text-anchor": "end" }, compactEuro(y1 * f)));
    });
    svg.appendChild(el("text", { x: L, y: H - 6, fill: CHART_COLORS.text }, opts.xFormat(x0)));
    svg.appendChild(el("text", { x: W - R, y: H - 6, fill: CHART_COLORS.text, "text-anchor": "end" }, opts.xFormat(x1)));
    svg.appendChild(el("text", { x: (L + W - R) / 2, y: H - 6, fill: CHART_COLORS.text, "text-anchor": "middle" }, opts.xLabel));
    (opts.refs || []).forEach(function (r) {
      if (!(r.y <= y1)) return;
      var y = py(r.y);
      svg.appendChild(el("line", { x1: L, x2: W - R, y1: y, y2: y, stroke: CHART_COLORS.ref, "stroke-width": 1, "stroke-dasharray": "3,3" }));
      svg.appendChild(el("text", { x: W - R - 2, y: y - 2, fill: CHART_COLORS.ref, "text-anchor": "end" }, r.label));
    });
    opts.series.forEach(function (s) {
      var d = "", pen = false;
      s.points.forEach(function (p) {
        if (!Number.isFinite(p[1])) { pen = false; return; }      // unknown: a gap, never drawn as 0
        d += (pen ? "L" : "M") + px(p[0]).toFixed(1) + " " + py(p[1]).toFixed(1);
        pen = true;
      });
      if (d) svg.appendChild(el("path", { d: d, fill: "none", stroke: s.color, "stroke-width": 2 }));
    });
    if (Number.isFinite(opts.marker) && opts.marker >= x0 && opts.marker <= x1) {
      var mx = px(opts.marker);
      svg.appendChild(el("line", { x1: mx, x2: mx, y1: T, y2: H - B, stroke: "#ffffff", "stroke-width": 1 }));
    }
    var lx = L;
    opts.series.forEach(function (s) {
      svg.appendChild(el("rect", { x: lx, y: 21, width: 8, height: 3, fill: s.color }));
      svg.appendChild(el("text", { x: lx + 11, y: 25, fill: CHART_COLORS.text }, s.name));
      lx += 14 + s.name.length * 5.2;
    });
    return svg;
  }
  function createController(terria, options) {
    options = options || {};
    var enabled = { hq10: false, hq100: false, hqextreme: false }, activeMap, activeId = null;
    var cache = new Map(), templates = new WeakSet(), last = null, pending = null, busy = false;
    function reset(scenario) {
      SCENARIOS.forEach(function (s) { enabled[s] = scenario === s; });
    }
    function installTemplate(item) {
      if (!item || templates.has(item) || typeof item.setTrait !== "function") return;
      var original = item.featureInfoTemplate || {};
      item.setTrait("user", "featureInfoTemplate", {
        name: original.name, formats: original.formats, partials: original.partials,
        template: "{{#fc_popup_ready}}" + BODY + "{{/fc_popup_ready}}{{^fc_popup_ready}}" +
          (original.template || "{{{terria.rawDataTable}}}") + "{{/fc_popup_ready}}"
      });
      templates.add(item);
    }
    var dynCache = new Map();
    function fetchJson(key, url, maxAgeMs) {
      var entry = dynCache.get(key), now = Date.now();
      if (entry && (entry.pending || now - entry.time < maxAgeMs)) return entry;
      entry = { time: now, data: null, pending: true };
      dynCache.set(key, entry);
      if (!options.fetch) { entry.pending = false; return entry; }
      Promise.resolve().then(function () {
        return options.fetch(options.backendUrl.replace(/\/$/, "") + url);
      }).then(function (response) { return response.json(); })
        .then(function (data) { entry.data = data; })
        .catch(function () { entry.data = { status: "unavailable", message: "Dynamic costs unavailable." }; })
        .then(function () { entry.pending = false; entry.time = Date.now(); tick(); });
      return entry;
    }
    // Model 1/2 at the slider level (stage curve), or Models 1-3 at the replay time (event).
    var dynShown = new Map();   // building -> {mode: "stage"|"event", stage | event + time}
    function dynamicValues(id, time) {
      dynShown.delete(id);
      var none = { fc_dyn_show: false, fc_dyn_stage: false, fc_dyn_time: false, fc_dyn_note: false };
      var water = shownWater(options.readHash ? options.readHash() : root.location && root.location.hash);
      var state = root.AlzetteTwin && typeof root.AlzetteTwin.getState === "function" ? root.AlzetteTwin.getState() : {};
      var result = Object.assign({}, none, { fc_dyn_show: true });
      if (state && state.replayDate || water.kind === "date") {
        var when = time && time.constructor && typeof time.constructor.toDate === "function" ? time.constructor.toDate(time) : null;
        if (!when) return none;
        var events = fetchJson("events", "/flood-costs/dynamic/events.json", 300000).data;
        if (!events) { result.fc_dyn_note = "Loading dynamic costs…"; return result; }
        var match = (events.events || []).filter(function (e) {
          return !e.stale && new Date(e.start_utc) <= when && when <= new Date(e.end_utc);
        })[0];
        if (!match) {
          result.fc_dyn_note = "No dynamic event has been computed for this replay time (task run_dynamic_event).";
          return result;
        }
        var minute = isoMinute(new Date(Math.floor(when.getTime() / 900000) * 900000));
        var data = fetchJson("e|" + match.event_id + "|" + id + "|" + minute, "/flood-costs/dynamic/event/" +
          encodeURIComponent(match.event_id) + "/" + encodeURIComponent(id) + ".json?time=" + encodeURIComponent(minute), 300000).data;
        if (!data) { result.fc_dyn_note = "Loading dynamic costs…"; return result; }
        if (data.status !== "success") { result.fc_dyn_note = data.message || "No dynamic cost for this building."; return result; }
        var at = data.value_at || {};
        result.fc_dyn_time = minute.replace("T", " ").slice(0, 16);
        result.fc_dyn_e1 = euros(at.model1_eur2010);
        result.fc_dyn_e2 = euros(at.model2_eur2010);
        result.fc_dyn_e3 = euros(at.model3_total_eur);
        dynShown.set(id, { mode: "event", event: match.event_id, time: when.getTime() });
        return result;
      }
      if (water.kind === "scenario") return none;          // the HQ maps: the static block above applies
      if (water.kind !== "level") return none;
      if (water.stage === null) {
        result.fc_dyn_note = "Dynamic costs are computed for the same level at every gauge: move the River slider to see them.";
        return result;
      }
      var stage = Math.max(0, Math.min(600, water.stage));
      var answer = fetchJson("s|" + id + "|" + stage, "/flood-costs/dynamic/stage/" + stage + "/" + encodeURIComponent(id) + ".json", 300000).data;
      if (!answer) { result.fc_dyn_note = "Loading dynamic costs…"; return result; }
      if (answer.status !== "success") { result.fc_dyn_note = answer.message || "No dynamic cost for this building."; return result; }
      result.fc_dyn_stage = String(answer.stage_cm);
      result.fc_dyn_m1 = euros(answer.model1_eur2010);
      result.fc_dyn_m2_6 = euros(answer.model2_6h_eur2010);
      result.fc_dyn_m2_24 = euros(answer.model2_24h_eur2010);
      result.fc_dyn_m2_72 = euros(answer.model2_72h_eur2010);
      if (answer.stale) result.fc_dyn_note = "Computed for an older static result; it is being recomputed.";
      dynShown.set(id, { mode: "stage", stage: answer.stage_cm });
      return result;
    }
    // Two charts under the "Dynamic flood costs" heading of the rendered popup:
    //   slider: this building's cost per water level, and the area's (with the HQ totals);
    //   replay: this building's cost over the event, and the area's.
    function chartsFor(id, shown) {
      var doc = options.document, euroDay = function (ms) { return new Date(ms).toISOString().slice(5, 16).replace("T", " "); };
      function pts(rows, xKey, yKey) {
        return rows.map(function (r) { return [xKey === "timestamp" ? Date.parse(r[xKey]) : r[xKey], r[yKey] === null ? NaN : r[yKey]]; });
      }
      if (shown.mode === "stage") {
        var b = fetchJson("bc|" + id, "/flood-costs/dynamic/building/" + encodeURIComponent(id) + "/curve.json", 300000).data;
        var a = fetchJson("ac", "/flood-costs/dynamic/curve.json", 300000).data;
        if (!b || !a) return null;
        var charts = [];
        var cm = function (v) { return v + " cm"; };
        if (b.status === "success") charts.push(lineChart(doc, { title: "This building: cost per water level", xLabel: "gauge level",
          xFormat: cm, marker: shown.stage, series: [
            { name: "Model 1", color: CHART_COLORS.model1, points: pts(b.stages, "stage_cm", "model1_eur2010") },
            { name: "Model 2 (24 h)", color: CHART_COLORS.model2, points: pts(b.stages, "stage_cm", "model2_24h_eur2010") }] }));
        if (a.status === "success") {
          var refs = Object.keys(a.static_totals_eur2010 || {}).map(function (k) {
            return { label: (k === "HQEXTREME" ? "HQextreme" : k) + " (static)", y: a.static_totals_eur2010[k] };
          });
          charts.push(lineChart(doc, { title: "Whole area: cost per water level" + (a.partial_reach ? " (part in this area)" : ""),
            xLabel: "gauge level", xFormat: cm, marker: shown.stage, refs: a.partial_reach ? [] : refs, series: [
              { name: "Model 1", color: CHART_COLORS.model1, points: pts(a.stages, "stage_cm", "model1_eur2010") },
              { name: "Model 2 (24 h)", color: CHART_COLORS.model2, points: pts(a.stages, "stage_cm", "model2_24h_eur2010") }] }));
        }
        return charts.filter(Boolean);
      }
      var ev = encodeURIComponent(shown.event);
      var be = fetchJson("be|" + shown.event + "|" + id, "/flood-costs/dynamic/event/" + ev + "/" + encodeURIComponent(id) + ".json", 300000).data;
      var ae = fetchJson("ae|" + shown.event, "/flood-costs/dynamic/event/" + ev + ".json", 300000).data;
      if (!be || !ae) return null;
      var out = [];
      [[be, "This building over the event"], [ae, "Whole area over the event"]].forEach(function (pair) {
        if (pair[0].status !== "success") return;
        out.push(lineChart(doc, { title: pair[1], xLabel: "UTC", xFormat: euroDay, marker: shown.time, series: [
          { name: "Model 1", color: CHART_COLORS.model1, points: pts(pair[0].timesteps, "timestamp", "model1_eur2010") },
          { name: "Model 2", color: CHART_COLORS.model2, points: pts(pair[0].timesteps, "timestamp", "model2_eur2010") },
          { name: "Model 3", color: CHART_COLORS.model3, points: pts(pair[0].timesteps, "timestamp", "model3_total_eur") }] }));
      });
      return out.filter(Boolean);
    }
    function placeCharts() {
      var doc = options.document;
      if (!doc || typeof doc.querySelectorAll !== "function") return;
      Array.prototype.forEach.call(doc.querySelectorAll("h3"), function (heading) {
        if (heading.textContent !== "Dynamic flood costs") return;
        // Which building this popup section is: the nearest ancestor whose text has
        // "Building ID:" followed by one of the ids the dynamic block was filled for.
        var block = heading.parentNode, id = null;
        for (var up = block, i = 0; up && i < 5 && !id; up = up.parentNode, i++) {
          var text = up.textContent || "";
          dynShown.forEach(function (value, key) {
            if (text.indexOf(key) >= 0 && /Building ID:/.test(text) && (!id || key.length > id.length)) id = key;
          });
          if (!id && /Building ID:/.test(text)) break;          // this section's building has no chart
        }
        var shown = id && dynShown.get(id);
        var existing = block.querySelector("[data-fc-charts]");
        var key = shown ? id + "|" + shown.mode + "|" + (shown.mode === "stage" ? shown.stage : shown.event + "|" + Math.floor(shown.time / 900000)) : "";
        if (existing && existing.getAttribute("data-fc-charts") === key) return;
        if (!shown) { if (existing) existing.parentNode.removeChild(existing); return; }
        var charts = chartsFor(id, shown);
        if (!charts) return;                                   // still loading; the next tick retries
        var box = doc.createElement("div");
        box.setAttribute("data-fc-charts", key);
        charts.forEach(function (chart) { box.appendChild(chart); });
        if (existing) existing.parentNode.removeChild(existing);
        block.appendChild(box);
      });
    }
    function request(id) {
      var entry = cache.get(id), now = Date.now();
      if (entry && now - entry.time < (entry.failed ? 30000 : 300000)) return entry;
      entry = { time: now, data: null, failed: false }; cache.set(id, entry);
      if (!options.fetch) { entry.failed = true; return entry; }
      entry.promise = Promise.resolve().then(function () {
        return options.fetch(options.backendUrl.replace(/\/$/, "") + "/flood-costs/popup/" + encodeURIComponent(id) + ".json");
      }).then(function (response) {
        if (!response.ok && response.status !== 404) throw new Error("Lookup unavailable");
        return response.json();
      }).then(function (payload) {
        if (!verifiedPayload(id, payload)) throw new Error("Lookup building ID or schema mismatch");
        entry.data = payload;
      }).catch(function () { entry.failed = true; }).then(tick);
      return entry;
    }
    function restoreSelection(time) {
      if (!pending || Date.now() > pending.until || (options.viewState && options.viewState.featureInfoPanelIsVisible === false)) {
        pending = null; return;
      }
      var current = buildingId(terria.selectedFeature, time);
      if (current && current !== pending.id) { pending = null; return; }
      var candidates = [];
      ((terria.workbench && terria.workbench.items) || []).forEach(function (item) {
        (item.mapItems || []).forEach(function (source) {
          ((source.entities && source.entities.values) || []).forEach(function (entity) {
            if (buildingId(entity, time) === pending.id) candidates.push({ item: item, entity: entity });
          });
        });
      });
      // A hashchange fires before Terria's asynchronous init has replaced the
      // workbench. Keep waiting while only the old layer/parts are present.
      var candidate = candidates.find(function (entry) { return pending.oldEntities.indexOf(entry.entity) < 0; });
      if (!candidate) return;
      var constructor = pending.feature.constructor;
      var feature = constructor && typeof constructor.fromEntityCollectionOrEntity === "function" ?
        constructor.fromEntityCollectionOrEntity(candidate.entity) : candidate.entity;
      feature._catalogItem = candidate.item;
      var picked = terria.pickedFeatures || pending.picked;
      if (!picked) return;
      picked.features = (picked.features || []).filter(function (f) { return buildingId(f, time) !== pending.id; }).concat([feature]);
      terria.pickedFeatures = picked;
      terria.selectedFeature = feature;
      pending = null;
    }
    function tick() {
      if (busy) return;
      busy = true;
      try {
        var time = terria.timelineClock && terria.timelineClock.currentTime;
        var scenario = options.readMapScenario ? options.readMapScenario() : mapScenario(options.document, terria);
        if (scenario !== activeMap) { activeMap = scenario; reset(scenario); }
        restoreSelection(time);
        var selected = terria.selectedFeature, id = buildingId(selected, time);
        if (id && id !== activeId) { activeId = id; reset(activeMap); }
        if (id) last = { id: id, feature: selected, picked: terria.pickedFeatures };
        else if (!pending) { activeId = null; last = null; }
        var features = ((terria.pickedFeatures && terria.pickedFeatures.features) || []).slice();
        if (selected && features.indexOf(selected) < 0) features.push(selected);
        features.forEach(function (feature) {
          var bid = buildingId(feature, time);
          if (!bid) return;
          var owner = ownerOf(terria, feature);
          // The 3D renderer recomputes footprint_area_m2 per exploded polygon;
          // a CZML part's area must never stand in for the financial building.
          var native = nativePayload(bid, properties(feature, time), !!owner && owner.type !== "czml");
          var entry = request(bid), payload = entry.data;
          if (!payload && native && entry.failed) {
            payload = native;
            payload.warnings.push("Using the existing monetary layer; backend lookup is unavailable.");
          }
          // API context is preferred. A loaded full-building monetary feature may
          // supplement it only when BOTH saved scenario amounts/statuses agree.
          if (payload && payload.found && !number(payload.footprint_area_m2, true) && native &&
              !(payload.warnings || []).length &&
              SCENARIOS.filter(function (s) { return payload[s]; }).every(function (s) { return native[s] && payload[s].damage_eur2010 === native[s].damage_eur2010 &&
                payload[s].monetary_status === native[s].monetary_status; })) {
            payload = Object.assign({}, payload, { footprint_area_m2: native.footprint_area_m2 });
            SCENARIOS.filter(function (s) { return payload[s]; }).forEach(function (s) { payload[s] = Object.assign({}, native[s], payload[s], {
              depth_mean_m: number(payload[s].depth_mean_m) ? payload[s].depth_mean_m : native[s].depth_mean_m,
              exposure_status: payload[s].exposure_status || native[s].exposure_status }); });
          }
          var values = Object.assign(display(bid, payload, enabled), dynamicValues(bid, time));
          Object.keys(values).forEach(function (key) { setProperty(feature, key, values[key], time); });
          installTemplate(owner);
        });
        if (options.onUpdate) options.onUpdate({ buildingid: id, hq10: enabled.hq10, hq100: enabled.hq100, hqextreme: enabled.hqextreme });
        placeCharts();
      } finally { busy = false; }
    }
    return {
      tick: tick,
      setScenario: function (scenario, checked) { if (SCENARIOS.indexOf(scenario) >= 0) enabled[scenario] = !!checked; tick(); },
      navigation: function () {
        if (!last) return;
        var oldEntities = [];
        ((terria.workbench && terria.workbench.items) || []).forEach(function (item) {
          (item.mapItems || []).forEach(function (source) {
            oldEntities = oldEntities.concat((source.entities && source.entities.values) || []);
          });
        });
        pending = Object.assign({}, last, { oldEntities: oldEntities, until: Date.now() + 15000 });
      },
      state: function () { return { buildingid: activeId, hq10: enabled.hq10, hq100: enabled.hq100, hqextreme: enabled.hqextreme }; }
    };
  }
  function findContext(document) {
    var nodes = document.querySelectorAll("body *"), fallback = null;
    for (var i = 0; i < nodes.length; i++) {
      var key = Object.keys(nodes[i]).find(function (k) { return /^__react(?:InternalInstance|Fiber)\$/.test(k); });
      for (var fiber = key && nodes[i][key]; fiber; fiber = fiber["return"]) {
        var props = fiber.memoizedProps;
        if (props && props.viewState && props.viewState.terria) return { terria: props.viewState.terria, viewState: props.viewState };
        if (props && props.terria && props.terria.timelineClock) fallback = { terria: props.terria };
      }
    }
    return fallback;
  }
  function comparisonPanel(document) {
    var panel = document.createElement("section"), boxes = {};
    panel.id = "eddie-flood-cost-comparison"; panel.hidden = true;
    panel.setAttribute("aria-label", "Flood-cost popup scenarios");
    panel.style.cssText = "position:fixed;right:12px;bottom:36px;z-index:1000;padding:10px;background:#fff;color:#222;border:1px solid #888;border-radius:5px;font:13px sans-serif;max-width:260px";
    var title = document.createElement("strong"); title.textContent = "Popup comparison"; panel.appendChild(title);
    SCENARIOS.forEach(function (s) {
      var label = document.createElement("label"), box = document.createElement("input");
      box.type = "checkbox"; box.id = "flood-cost-popup-" + s; label.style.marginLeft = "10px";
      label.appendChild(box); label.appendChild(document.createTextNode(" " + (s === "hqextreme" ? "HQextreme" : s.toUpperCase()))); panel.appendChild(label); boxes[s] = box;
    });
    var note = document.createElement("p"); note.textContent = "Popup only · map controls are unchanged"; note.style.marginBottom = "0"; panel.appendChild(note);
    document.body.appendChild(panel);
    return { panel: panel, boxes: boxes };
  }
  function start() {
    var context = null, controller = null, panel = comparisonPanel(root.document);
    function poll() {
      context = context || findContext(root.document);
      if (!context) return;
      if (!controller) {
        controller = createController(context.terria, {
          document: root.document, viewState: context.viewState,
          backendUrl: "$BACKEND_URL", fetch: root.fetch && root.fetch.bind(root),
          onUpdate: function (state) {
            panel.panel.hidden = !state.buildingid || !!(context.viewState && context.viewState.featureInfoPanelIsVisible === false);
            SCENARIOS.forEach(function (s) { panel.boxes[s].checked = state[s]; });
          }
        });
        SCENARIOS.forEach(function (s) { panel.boxes[s].addEventListener("change", function () { controller.setScenario(s, this.checked); }); });
        root.addEventListener("alzette-twin-change", function () { controller.tick(); });
        root.addEventListener("hashchange", function () { controller.navigation(); controller.tick(); });
        root.document.addEventListener("change", function (event) {
          if (/^water-level-control-hq(?:10|100|extreme)$/.test(event.target.id)) Promise.resolve().then(controller.tick);
        });
      }
      controller.tick();
    }
    root.setInterval(poll, 300);
    poll();
  }
  root.EddieFloodCostPopup = { createController: createController, display: display, money: money,
    nativePayload: nativePayload, buildingId: buildingId, mapScenario: mapScenario, template: BODY,
    shownWater: shownWater, lineChart: lineChart };
  if (root.document) {
    if (root.document.readyState === "loading") root.document.addEventListener("DOMContentLoaded", start);
    else start();
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
