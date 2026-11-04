// Water-level control for the Alzette 3D water surface.
//
// Loads a Terria init file from the backend through the URL hash, the same way
// the Digital Twin date selector does. Each new level animates from the previous one.
// By default the water shows the latest gauge readings stored in the database
// (GET /water-level/latest); the HQ10 / HQ100 tick boxes override them while ticked.
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
  // Active flood scenario, and the level to return to when it is unticked.
  var scenario = null;
  var baseLevel = null;
  // True while the water follows the latest gauge readings.
  var live = true;
  var liveLevel = null;
  var ui = {};
  // Gauge stages of the day being replayed, shown in the inputs as Terria's clock plays it.
  var replayStages = null;
  var terria = null;

  function loadInit(path) {
    currentView = path;
    var prefix = showBuildings ? "buildings/" : "";
    window.location.hash = BACKEND_URL + "/terria-water-init/" + prefix + path + ".json";
  }

  // Without a Livange stage, the Hesperange stage is used: the backend then holds it upstream.
  function animateTo(pfaffenthal, hesperange, livange) {
    if (livange === null || livange === undefined || livange === "") {
      livange = hesperange;
    }
    var pf = previous ? previous.pf + "," + pfaffenthal : String(pfaffenthal);
    var he = previous ? previous.he + "," + hesperange : String(hesperange);
    var li = previous ? previous.li + "," + livange : String(livange);
    previous = { pf: pfaffenthal, he: hesperange, li: livange };
    loadInit("level/" + pf + "/" + he + "/" + li);
  }

  // A gauge level chosen by hand, or the live one; unticks any scenario.
  function applyLevel(pfaffenthalCm, hesperangeCm, livangeCm) {
    replayStages = null;
    baseLevel = { pf: pfaffenthalCm, he: hesperangeCm, li: livangeCm };
    setScenarioBoxes(null);
    scenario = null;
    setMode(live ? "LIVE" : "MANUAL");
    animateTo(pfaffenthalCm, hesperangeCm, livangeCm);
    updateReportLink();
  }

  function setScenario(name) {
    replayStages = null;
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
      window.location.hash = BACKEND_URL + "/terria-water-init/buildings.json";
    }
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

  var MODE_COLOURS = { LIVE: "#2e9d5b", MANUAL: "#4a7bd0", REPLAY: "#8a63c9", HQ10: "#e08a1e", HQ100: "#d0412f" };

  // Badge in the panel header: what the water shows now.
  function setMode(mode) {
    ui.mode.textContent = mode;
    ui.mode.style.background = MODE_COLOURS[mode] || "#555";
  }

  var STYLE = [
    "#water-level-control{position:fixed;top:72px;right:64px;z-index:1000;width:290px;max-width:calc(100vw - 32px);",
    "max-height:calc(100vh - 170px);overflow:auto;background:rgba(32,38,52,0.95);color:#e8ecf2;border-radius:8px;",
    "box-shadow:0 4px 16px rgba(0,0,0,0.35);font:13px/1.4 Nunito,\"Segoe UI\",sans-serif}",
    "#water-level-control .wl-head{display:flex;align-items:center;gap:8px;padding:10px 12px;cursor:pointer;",
    "border-bottom:1px solid rgba(255,255,255,0.08);position:sticky;top:0;background:rgb(32,38,52)}",
    "#water-level-control .wl-title{font-weight:700;flex:1}",
    "#water-level-control .wl-mode{font-size:11px;font-weight:700;letter-spacing:.05em;padding:2px 8px;border-radius:10px;color:#fff}",
    "#water-level-control .wl-chevron{opacity:.7;transition:transform .15s}",
    "#water-level-control.wl-collapsed .wl-chevron{transform:rotate(-90deg)}",
    "#water-level-control.wl-collapsed .wl-body{display:none}",
    "#water-level-control .wl-body{padding:0 12px 6px}",
    "#water-level-control .wl-section{padding:10px 0;border-bottom:1px solid rgba(255,255,255,0.08)}",
    "#water-level-control .wl-section:last-child{border-bottom:0}",
    "#water-level-control h4{margin:0 0 6px;font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;opacity:.65}",
    "#water-level-control .wl-note{font-size:12px;opacity:.8;margin-top:6px}",
    "#water-level-control .wl-note:empty{display:none}",
    "#water-level-control table{width:100%;border-collapse:collapse}",
    "#water-level-control td{padding:3px 0}",
    "#water-level-control td.wl-km{opacity:.6;font-size:12px;text-align:right;padding-right:8px}",
    "#water-level-control td.wl-input{width:90px}",
    "#water-level-control input[type=number],#water-level-control input[type=date]{width:100%;box-sizing:border-box;",
    "background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.15);border-radius:4px;color:inherit;",
    "padding:3px 6px;font:inherit;color-scheme:dark}",
    "#water-level-control input[type=range]{width:100%;margin:8px 0 6px}",
    "#water-level-control .wl-row{display:flex;gap:6px}",
    "#water-level-control button{flex:1;cursor:pointer;border:0;border-radius:4px;padding:6px 8px;font:inherit;",
    "font-weight:600;background:#3f6fb5;color:#fff;white-space:nowrap}",
    "#water-level-control .wl-row input[type=date]{flex:1;min-width:0}",
    "#water-level-control .wl-row input[type=date]+button{flex:0 0 auto}",
    "#water-level-control button.wl-secondary{background:rgba(255,255,255,0.12)}",
    "#water-level-control button:hover{filter:brightness(1.12)}",
    "#water-level-control .wl-toggle{display:flex;align-items:center;gap:8px;padding:3px 0;cursor:pointer}",
    "#water-level-control .wl-toggle input{accent-color:#3f6fb5;width:15px;height:15px;margin:0}",
    "#water-level-control a{color:#8cc8ff;display:inline-block;margin-top:6px}",
    "#water-level-control .wl-legend{display:flex;gap:10px;flex-wrap:wrap;font-size:12px;opacity:.85;margin-top:6px}",
    "#water-level-control .wl-swatch{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}"
  ].join("");

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

  function build() {
    var style = document.createElement("style");
    style.textContent = STYLE;
    document.head.appendChild(style);

    var panel = element("div");
    panel.id = "water-level-control";

    // Header: title, what the water shows, collapse.
    var head = element("div", "wl-head");
    head.title = "Show or hide";
    head.appendChild(element("span", "wl-title", "Alzette water"));
    ui.mode = element("span", "wl-mode");
    head.appendChild(ui.mode);
    head.appendChild(element("span", "wl-chevron", "▾"));
    head.addEventListener("click", function () {
      panel.classList.toggle("wl-collapsed");
      try {
        localStorage.setItem("wl-collapsed", panel.classList.contains("wl-collapsed") ? "1" : "0");
      } catch (e) { /* storage unavailable */ }
    });
    panel.appendChild(head);
    // Start collapsed when asked last time, or on a narrow screen where it would cover the map.
    var collapsed = window.innerWidth < 600;
    try {
      var stored = localStorage.getItem("wl-collapsed");
      if (stored !== null) {
        collapsed = stored === "1";
      }
    } catch (e) { /* storage unavailable */ }
    if (collapsed) {
      panel.classList.add("wl-collapsed");
    }

    var body = element("div", "wl-body");
    panel.appendChild(body);

    // Gauges.
    var gauges = section(body, "Gauge stage (cm above gauge zero)");
    var table = element("table");
    ui.pf = numberInput();
    ui.he = numberInput();
    ui.li = numberInput();
    [["Pfaffenthal", "km 35.0", ui.pf], ["Hesperange", "km 45.4", ui.he], ["Livange", "km 54.3", ui.li]].forEach(function (row) {
      var tr = element("tr");
      tr.appendChild(element("td", null, row[0]));
      tr.appendChild(element("td", "wl-km", row[1]));
      var cell = element("td", "wl-input");
      cell.appendChild(row[2]);
      tr.appendChild(cell);
      table.appendChild(tr);
    });
    gauges.appendChild(table);

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
    actions.appendChild(button("Live level", function () {
      live = true;
      liveLevel = null;
      refreshLive();
    }, true));
    gauges.appendChild(actions);

    ui.liveInfo = element("div", "wl-note", "Loading live gauge data...");
    ui.liveInfo.id = "water-level-control-live";
    gauges.appendChild(ui.liveInfo);

    // Flood scenarios.
    var scenarios = section(body, "Flood scenarios");
    var hq10 = checkbox("water-level-control-hq10", "HQ10 – 10-year flood", function (checked) {
      setScenario(checked ? "hq10" : null);
    });
    var hq100 = checkbox("water-level-control-hq100", "HQ100 – 100-year flood", function (checked) {
      setScenario(checked ? "hq100" : null);
    });
    ui.hq10 = hq10.box;
    ui.hq100 = hq100.box;
    scenarios.appendChild(hq10.label);
    scenarios.appendChild(hq100.label);
    ui.report = element("a");
    ui.report.id = "water-level-control-report";
    ui.report.target = "_blank";
    ui.report.rel = "noopener";
    ui.report.style.display = "none";
    scenarios.appendChild(ui.report);

    // Day replay.
    var replay = section(body, "Replay a day");
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
          loadInit("date/" + selected);
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

    // Layers.
    var layers = section(body, "Layers");
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

    document.body.appendChild(panel);
    setMode("LIVE");

    refreshLive();
    setInterval(refreshLive, LIVE_POLL_MS);
    setInterval(showReplayStages, 500);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", build);
  } else {
    build();
  }
})();
