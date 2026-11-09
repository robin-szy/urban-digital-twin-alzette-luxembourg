/* Additive TerriaJS popup adapter. No map layers, water controls or data are replaced.
 * $BACKEND_URL is substituted by the existing frontend entrypoint.
 * The comparison switches affect feature information only; map controls remain exclusive.
 */
(function (root) {
  "use strict";
  var SCENARIOS = ["hq10", "hq100"];
  var PREFIX = "fc_popup_";
  var BODY = '<h3>Building Flood Damage</h3><p><strong>Provisional · 2010 EUR</strong></p>' +
    '<p><strong>Building ID:</strong> {{fc_popup_id}}<br>' +
    '<strong>Building category:</strong> {{fc_popup_category}}<br>' +
    '<strong>Building footprint area:</strong> {{fc_popup_area}}</p>' +
    SCENARIOS.map(function (s) {
      return '{{#fc_popup_' + s + '_show}}<h4>' + s.toUpperCase() + '</h4>' +
        '<p><strong>Estimated flood damage: {{fc_popup_' + s + '_damage}}</strong><br>' +
        'Estimated flood depth (wet-sample mean): {{fc_popup_' + s + '_depth}}<br>' +
        'Flood exposure: {{fc_popup_' + s + '_exposure}}</p>{{/fc_popup_' + s + '_show}}';
    }).join("") +
    '{{#fc_popup_no_scenario}}<p>No HQ damage scenario selected.</p>{{/fc_popup_no_scenario}}' +
    '{{#fc_popup_comparing}}<p>Alternative flood scenarios; amounts are not additive.</p>{{/fc_popup_comparing}}' +
    '{{#fc_popup_warning}}<p><strong>Data quality:</strong> {{fc_popup_warning}}</p>{{/fc_popup_warning}}';

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
        values.damage_estimates_provisional !== true || !SCENARIOS.every(function (s) {
      return Object.prototype.hasOwnProperty.call(values, "damage_" + s + "_status");
    })) return null;
    var accepted = values.accepted_provisional_classification === true;
    var result = { buildingid: id, found: true, category: accepted ? values.jrc_category : "Unknown",
      currency: "EUR", price_year: 2010, provisional: true, warnings: [],
      footprint_area_m2: allowWholeArea !== false && values.monetary_method && number(values.footprint_part_count, true) ? values.footprint_area_m2 : null };
    SCENARIOS.forEach(function (s) {
      result[s] = { damage_eur2010: accepted ? values["damage_" + s + "_eur2010"] : null,
        monetary_status: values["damage_" + s + "_status"],
        depth_mean_m: values["depth_" + s + "_mean_m"], exposure_status: values["exposure_" + s + "_status"],
        quality_flags: String(values["damage_" + s + "_quality_flags"] || "").split("|").filter(Boolean) };
    });
    return result;
  }
  function verifiedPayload(id, payload) {
    return payload && payload.buildingid === id && typeof payload.found === "boolean" &&
      payload.currency === "EUR" && payload.price_year === 2010 && SCENARIOS.every(function (s) { return payload[s]; });
  }
  function display(id, payload, enabled) {
    var matched = verifiedPayload(id, payload) && payload.found;
    var warnings = matched && Array.isArray(payload.warnings) ? payload.warnings.slice() : [];
    var category = matched && ["Residential", "Commercial", "Industrial"].indexOf(payload.category) >= 0 ? payload.category : "Unknown";
    var result = { fc_popup_ready: true, fc_popup_id: escapeText(id), fc_popup_category: category,
      fc_popup_area: matched ? format(payload.footprint_area_m2, " m²", true) : "N/A",
      fc_popup_no_scenario: !enabled.hq10 && !enabled.hq100, fc_popup_comparing: enabled.hq10 && enabled.hq100 };
    if (!matched) warnings.push("No verified flood-cost record is available for this building.");
    var exposure = { none: "Not exposed", touched_or_partial: "Touched or partly exposed", fully_contained: "Fully within the flood extent" };
    SCENARIOS.forEach(function (s) {
      var cost = matched ? payload[s] : null;
      result[PREFIX + s + "_show"] = !!enabled[s];
      result[PREFIX + s + "_damage"] = money(cost);
      result[PREFIX + s + "_depth"] = cost ? format(cost.depth_mean_m, " m") : "N/A";
      result[PREFIX + s + "_exposure"] = cost ? exposure[cost.exposure_status] || "N/A" : "N/A";
      if (!enabled[s] || !cost) return;
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
    var boxes = SCENARIOS.map(function (s) { return document && document.getElementById("water-level-control-" + s); });
    if (boxes.some(Boolean)) {
      return boxes[1] && boxes[1].checked ? "hq100" : boxes[0] && boxes[0].checked ? "hq10" : null;
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
  function createController(terria, options) {
    options = options || {};
    var enabled = { hq10: false, hq100: false }, activeMap, activeId = null;
    var cache = new Map(), templates = new WeakSet(), last = null, pending = null, busy = false;
    function reset(scenario) {
      enabled.hq10 = scenario === "hq10"; enabled.hq100 = scenario === "hq100";
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
              SCENARIOS.every(function (s) { return payload[s].damage_eur2010 === native[s].damage_eur2010 &&
                payload[s].monetary_status === native[s].monetary_status; })) {
            payload = Object.assign({}, payload, { footprint_area_m2: native.footprint_area_m2 });
            SCENARIOS.forEach(function (s) { payload[s] = Object.assign({}, native[s], payload[s], {
              depth_mean_m: number(payload[s].depth_mean_m) ? payload[s].depth_mean_m : native[s].depth_mean_m,
              exposure_status: payload[s].exposure_status || native[s].exposure_status }); });
          }
          var values = display(bid, payload, enabled);
          Object.keys(values).forEach(function (key) { setProperty(feature, key, values[key], time); });
          installTemplate(owner);
        });
        if (options.onUpdate) options.onUpdate({ buildingid: id, hq10: enabled.hq10, hq100: enabled.hq100 });
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
      state: function () { return { buildingid: activeId, hq10: enabled.hq10, hq100: enabled.hq100 }; }
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
      label.appendChild(box); label.appendChild(document.createTextNode(" " + s.toUpperCase())); panel.appendChild(label); boxes[s] = box;
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
    nativePayload: nativePayload, buildingId: buildingId, mapScenario: mapScenario, template: BODY };
  if (root.document) {
    if (root.document.readyState === "loading") root.document.addEventListener("DOMContentLoaded", start);
    else start();
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
