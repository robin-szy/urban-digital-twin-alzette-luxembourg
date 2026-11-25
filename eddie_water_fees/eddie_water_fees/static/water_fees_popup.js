/* Per-building water fees inside Terria's Feature Information card.
 *
 * Served by the eddie_water_fees plugin (<backend>/water-fees/popup.js). When a building is
 * clicked, this adds one section at the top of the Feature Information card with the price of
 * Pegah's five scenarios separately and their sum. It adds no map layer, replaces no
 * feature-info template and touches neither the other popups nor the shared panels.
 *
 * The prices come from the plugin itself:
 * GET <backend>/water-fees/building/<building id>.json
 * (eddie_water_fees.blueprint.water_fees_building).
 */
(function (root) {
  "use strict";

  // The plugin's own origin: this file is loaded from <backend>/water-fees/popup.js.
  var BACKEND_URL = (function () {
    var current = root.document && root.document.currentScript;
    var src = (current && current.src) || "";
    return src.replace(/\/water-fees\/popup\.js.*$/, "");
  })();

  var SECTION_ID = "water-fees-feature-info";
  // The Feature Information card is a <ul> of sections; ours is one of its <li>s.
  var PANEL_BODY = ".tjs-feature-info-panel__body";
  var CACHE_MS = 30000;
  var POLL_MS = 400;

  var cache = new Map();
  var shownId = null;
  var signature = null;
  var section = null;
  var observer = null;
  var observedList = null;

  // ---------------------------------------------------------------- Terria

  // Terria bundles its state in the mounted React tree; the panel finds it the same way the
  // other popups do, without importing anything from the page.
  function findTerria(document) {
    var nodes = document.querySelectorAll("body *");
    for (var i = 0; i < nodes.length; i++) {
      var key = Object.keys(nodes[i]).find(function (name) {
        return /^__react(?:InternalInstance|Fiber)\$/.test(name);
      });
      for (var fiber = key && nodes[i][key]; fiber; fiber = fiber["return"]) {
        var props = fiber.memoizedProps;
        if (!props) continue;
        if (props.terria && props.terria.timelineClock) return props.terria;
        if (props.viewState && props.viewState.terria) return props.viewState.terria;
      }
    }
    return null;
  }

  function properties(feature, time) {
    try {
      if (!feature || !feature.properties) return {};
      var bag = feature.properties;
      if (typeof bag.getValue === "function") return bag.getValue(time) || {};
      return bag || {};
    } catch (error) {
      return {};
    }
  }

  // "ACT_...", both on the 3D buildings (buildingid) and in the table (gml_id, prefixed).
  function buildingId(feature, time) {
    var values = properties(feature, time);
    var id = values.buildingid || values.gml_id || "";
    if (typeof id !== "string") return null;
    id = id.replace(/^Building2D\./, "");
    return /^ACT_[A-Za-z0-9-]{6,}$/.test(id) ? id : null;
  }

  function selectedBuilding(terria) {
    var time = terria.timelineClock && terria.timelineClock.currentTime;
    var id = buildingId(terria.selectedFeature, time);
    if (id) return id;
    var features = (terria.pickedFeatures && terria.pickedFeatures.features) || [];
    for (var i = 0; i < features.length; i++) {
      id = buildingId(features[i], time);
      if (id) return id;
    }
    return null;
  }

  // ---------------------------------------------------------------- the card

  function element(name, className, text) {
    var node = root.document.createElement(name);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function euro(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) return "–";
    return "€" + new Intl.NumberFormat("en-IE", {
      minimumFractionDigits: 2, maximumFractionDigits: 2
    }).format(value);
  }

  // The Feature Information card re-renders with Terria's React tree, so our <li> is looked for
  // on every tick and put back at the top of the list when React has dropped it.
  function panelList() {
    var lists = root.document.querySelectorAll(PANEL_BODY);
    return lists.length ? lists[lists.length - 1] : null;
  }

  function buildSection() {
    var list = element("li", "tjs-feature-info-section__section tjs-_base__clearfix");
    list.id = SECTION_ID;

    var header = element("div", "tjs-feature-info-section__title");
    var title = element("span", null, "Water fee · 5 scenarios");
    header.appendChild(title);
    list.appendChild(header);

    var content = element("section", "tjs-feature-info-section__content");
    content.style.cssText = "padding:6px 8px";
    var table = element("table");
    table.style.cssText = "width:100%;font-family:inherit;border-collapse:collapse";
    content.appendChild(table);
    var note = element("p");
    note.style.cssText = "margin:6px 0 0;font-size:.8rem;color:#9ca3af;line-height:1.35";
    content.appendChild(note);
    list.appendChild(content);

    return { node: list, table: table, note: note, title: title };
  }

  function cell(text, amount) {
    var td = element("td", null, text);
    td.style.cssText = "border:0;padding:3px 0;vertical-align:top" +
      (amount
        ? ";text-align:right;white-space:nowrap;padding-left:10px;font-variant-numeric:tabular-nums"
        : "");
    return td;
  }

  function row(text, amount, options) {
    options = options || {};
    var tr = element("tr");
    if (options.total) tr.style.cssText = "border-top:1px solid #6b7280;font-weight:600";
    tr.appendChild(cell(text));
    tr.appendChild(cell(amount, true));
    return tr;
  }

  function detailRow(text) {
    var tr = element("tr");
    var td = cell(text);
    td.colSpan = 2;
    td.style.cssText = "border:0;padding:0 0 6px;font-size:.8rem;color:#9ca3af";
    tr.appendChild(td);
    return tr;
  }

  function render(data) {
    if (!section) return;
    var next = JSON.stringify(data);
    if (next === signature) return;
    signature = next;

    var scenarios = (data && data.scenarios) || [];
    section.title.textContent = "Water fee · " + (scenarios.length || 5) + " scenarios";
    section.table.textContent = "";
    scenarios.forEach(function (scenario) {
      section.table.appendChild(row(scenario.name, euro(scenario.cost_eur)));
      if (scenario.detail) section.table.appendChild(detailRow(scenario.detail));
    });
    section.table.appendChild(row("Total (all " + scenarios.length + " scenarios)",
      euro(data.total_eur), { total: true }));

    var notes = [];
    if (data.scope && data.scope.reach_id) {
      notes.push("Scope " + data.scope.reach_id + " / " + data.scope.variant_key);
    }
    if (data.computed_utc) {
      notes.push("run " + String(data.computed_utc).replace("T", " ").slice(0, 16) + " UTC");
    }
    if (data.note) notes.push(data.note);
    section.note.textContent = notes.join(" · ");
  }

  function renderMessage(text) {
    if (!section) return;
    signature = JSON.stringify({ message: text });
    section.table.textContent = "";
    section.note.textContent = text;
  }

  function removeSection() {
    if (section && section.node.parentNode) section.node.parentNode.removeChild(section.node);
    section = null;
    signature = null;
  }

  // Keep the section as the first <li> of the Feature Information card.
  function placeSection() {
    var list = panelList();
    if (!list) return null;
    if (list !== observedList) {
      if (observer) observer.disconnect();
      observedList = list;
      if (root.MutationObserver) {
        observer = new root.MutationObserver(function () {
          if (section && !list.contains(section.node)) list.insertBefore(section.node, list.firstChild);
        });
        observer.observe(list, { childList: true });
      }
    }
    if (!section) section = buildSection();
    if (section.node.parentNode !== list || list.firstChild !== section.node) {
      list.insertBefore(section.node, list.firstChild);
    }
    return section;
  }

  function load(id) {
    var entry = cache.get(id);
    if (!entry || Date.now() - entry.time >= CACHE_MS) {
      renderMessage("Loading water-fee prices…");
      entry = { time: Date.now(), data: null };
      cache.set(id, entry);
      entry.promise = root.fetch(BACKEND_URL + "/water-fees/building/" +
        encodeURIComponent(id) + ".json")
        .then(function (response) { return response.json(); })
        .catch(function () {
          return { status: "error", message: "Could not reach the backend." };
        })
        .then(function (data) {
          entry.time = Date.now();
          entry.data = data;
          if (shownId === id && section) {
            if (data.status === "success") render(data);
            else renderMessage(data.message || "No water-fee result for this building yet.");
          }
          return data;
        });
      return;
    }
    if (entry.data) {
      if (entry.data.status === "success") render(entry.data);
      else renderMessage(entry.data.message || "No water-fee result for this building yet.");
    } else {
      renderMessage("Loading water-fee prices…");
    }
  }

  function tick() {
    var document = root.document;
    var terria = findTerria(document);
    if (!terria) return;
    var id = selectedBuilding(terria);
    if (!id) {
      if (shownId) {
        shownId = null;
        removeSection();
      }
      return;
    }
    if (!placeSection()) return;
    if (id !== shownId) {
      shownId = id;
      load(id);
    }
  }

  root.EddieWaterFeesPopup = {
    tick: tick,
    buildingId: buildingId,
    backendUrl: BACKEND_URL,
    section: function () { return section && section.node; }
  };

  function start() {
    root.setInterval(tick, POLL_MS);
    tick();
  }

  if (!root.document) return;
  if (root.document.readyState === "loading") {
    root.document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
