/* Dependency-free controller tests; Node and the macOS JavaScriptCore CLI work. */
if (typeof require === "function") {
  require("vm").runInThisContext(require("fs").readFileSync(process.argv[2], "utf8"));
}
(function () {
  "use strict";
  var api = globalThis.EddieFloodCostPopup, count = 0;
  function assert(value, message) { if (!value) throw new Error(message || "Assertion failed"); }
  function equal(a, b, message) { assert(JSON.stringify(a) === JSON.stringify(b), message || JSON.stringify(a) + " != " + JSON.stringify(b)); }
  function cost(amount, status) { return { damage_eur2010: amount, monetary_status: status,
    depth_mean_m: .75, exposure_status: "touched_or_partial", quality_flags: [] }; }
  function payload(id, a, b) { return { buildingid: id, found: true, currency: "EUR", price_year: 2010,
    category: "Residential", provisional: true, footprint_area_m2: 321.75, warnings: [],
    hq10: cost(a, a === 0 ? "calculated_zero" : "calculated_positive"),
    hq100: cost(b, b === 0 ? "calculated_zero" : "calculated_positive") }; }
  function feature(id, entityId) {
    var values = { buildingid: id, samples_total_hq10: 999 }, bag = {}, changes = 0;
    bag.getValue = function () { return Object.assign({}, values); };
    bag.addProperty = function (key, value) {
      values[key] = value;
      bag[key] = { setValue: function (next) { values[key] = next; changes++; } };
    };
    return { id: entityId || "building-7", properties: bag, values: values, changes: function () { return changes; } };
  }
  function layer(features) {
    return { type: "czml", url: "/buildings-3d.czml?scenario=hq10", uniqueId: "buildings",
      featureInfoTemplate: { template: "<p>{{original_field}}</p>" },
      mapItems: [{ entities: { values: features, contains: function (f) { return features.indexOf(f) >= 0; } } }],
      setTrait: function (stratum, name, value) { assert(stratum === "user"); this[name] = value; } };
  }
  function setup(id, options) {
    options = options || {};
    var f = feature(id), item = layer([f]), map = "hq10";
    f._catalogItem = item;
    var terria = { selectedFeature: f, pickedFeatures: { features: [f] },
      timelineClock: { currentTime: {} }, workbench: { items: [item] } };
    var requested = [];
    var controller = api.createController(terria, {
      backendUrl: "https://example.invalid", readMapScenario: function () { return map; },
      viewState: options.viewState,
      fetch: options.fetch || function (url) {
        requested.push(url);
        return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(payload(id, 13573.35, 20000.)); } });
      }
    });
    return { feature: f, layer: item, terria: terria, controller: controller, requested: requested,
      map: function (next) { map = next; } };
  }
  async function flush() { for (var i = 0; i < 12; i++) await Promise.resolve(); }
  async function test(name, run) { await run(); count++; }
  async function run() {
    await test("currency and valid zero", function () {
      equal(api.money(cost(13573.35, "calculated_positive")), "€13,573.35");
      equal(api.money(cost(0, "calculated_zero")), "€0.00");
      equal(api.money(cost(-0, "calculated_zero")), "€0.00");
      [null, -1, NaN, "13573.35"].forEach(function (v) { equal(api.money(cost(v, "calculated_positive")), "N/A"); });
      equal(api.money(cost(0, "unknown_depth_coverage")), "N/A");
      equal(api.money(cost(0, "excluded_outside_corridor")), "N/A");
    });
    await test("original ACT property only", function () {
      equal(api.buildingId(feature("ACT_ORIGINAL", "building-99")), "ACT_ORIGINAL");
      equal(api.buildingId({ id: "ACT_OTHER", properties: { getValue: function () { return {}; } } }), null);
    });
    await test("mismatched and absent records cannot supply another building", function () {
      var values = api.display("ACT_A", payload("ACT_B", 100, 200), { hq10: true, hq100: true });
      equal(values.fc_popup_hq10_damage, "N/A"); equal(values.fc_popup_area, "N/A");
      var absent = payload("ACT_A", 100, 200); absent.found = false;
      equal(api.display("ACT_A", absent, { hq10: true }).fc_popup_hq10_damage, "N/A");
    });
    await test("no invented depth; whole-building area and separate scenarios", function () {
      var p = payload("ACT_A", 13573.35, 20000); p.hq100.depth_mean_m = null;
      var values = api.display("ACT_A", p, { hq10: true, hq100: true });
      equal(values.fc_popup_area, "321.75 m²"); equal(values.fc_popup_hq100_depth, "N/A");
      assert(values.fc_popup_hq10_show && values.fc_popup_hq100_show && values.fc_popup_comparing);
      assert(!Object.keys(values).some(function (k) { return /total|sum/.test(k); }));
    });
    await test("popup defaults to active HQ10 and fetches by ACT id", async function () {
      var s = setup("ACT_A"); s.controller.tick(); await flush();
      equal(s.controller.state(), { buildingid: "ACT_A", hq10: true, hq100: false });
      equal(s.feature.values.fc_popup_hq10_damage, "€13,573.35");
      assert(s.requested[0].endsWith("/flood-costs/popup/ACT_A.json"));
      assert(s.requested[0].indexOf("building-7") < 0);
    });
    await test("popup opens with HQ100 when HQ100 is already selected", async function () {
      var s = setup("ACT_A"); s.map("hq100"); s.controller.tick(); await flush();
      equal(s.controller.state(), { buildingid: "ACT_A", hq10: false, hq100: true });
    });
    await test("no selected map HQ scenario starts with building information only", async function () {
      var s = setup("ACT_A"); s.map(null); s.controller.tick(); await flush();
      assert(s.feature.values.fc_popup_no_scenario);
      assert(!s.feature.values.fc_popup_hq10_show && !s.feature.values.fc_popup_hq100_show);
    });
    await test("all four popup toggle combinations without touching map state", async function () {
      var s = setup("ACT_A"); s.controller.tick(); await flush();
      s.controller.setScenario("hq100", true);
      equal(s.controller.state(), { buildingid: "ACT_A", hq10: true, hq100: true });
      s.controller.setScenario("hq10", false);
      equal(s.controller.state(), { buildingid: "ACT_A", hq10: false, hq100: true });
      s.controller.setScenario("hq100", false);
      equal(s.controller.state(), { buildingid: "ACT_A", hq10: false, hq100: false });
      assert(s.feature.values.fc_popup_no_scenario);
      equal(s.layer.url, "/buildings-3d.czml?scenario=hq10");
      equal(s.requested.length, 1);
    });
    await test("open popup updates and map changes reset comparison defaults", async function () {
      var s = setup("ACT_A"); s.controller.tick(); await flush();
      s.controller.setScenario("hq100", true);
      assert(s.feature.changes() > 0);
      s.map("hq100"); s.controller.tick();
      equal(s.controller.state(), { buildingid: "ACT_A", hq10: false, hq100: true });
      equal(s.feature.values.fc_popup_hq100_damage, "€20,000.00");
      s.map(null); s.controller.tick(); assert(s.feature.values.fc_popup_no_scenario);
    });
    await test("new building starts with current map default", async function () {
      var s = setup("ACT_A"); s.controller.tick(); await flush(); s.controller.setScenario("hq100", true);
      var b = feature("ACT_B"); b._catalogItem = s.layer;
      s.terria.selectedFeature = b; s.terria.pickedFeatures.features = [b]; s.controller.tick();
      equal(s.controller.state(), { buildingid: "ACT_B", hq10: true, hq100: false });
      equal(b.values.fc_popup_hq10_damage, "N/A");
    });
    await test("preserve original raw properties, templates and map layers", async function () {
      var s = setup("ACT_A"), sources = s.layer.mapItems;
      s.controller.tick(); await flush();
      equal(s.feature.values.samples_total_hq10, 999);
      assert(s.layer.featureInfoTemplate.template.indexOf("{{original_field}}") >= 0);
      assert(s.layer.featureInfoTemplate.template.indexOf("featureInfo") < 0);
      assert(s.layer.mapItems === sources && s.terria.workbench.items.length === 1);
    });
    await test("reuse monetary layer with whole-building area; reject wrong currency", function () {
      var values = { buildingid: "ACT_A", damage_currency: "EUR", damage_price_year: 2010,
        damage_estimates_provisional: true, accepted_provisional_classification: true, jrc_category: "Commercial",
        footprint_area_m2: 321.75, footprint_part_count: 2, monetary_method: "saved validated method",
        damage_hq10_status: "calculated_zero", damage_hq10_eur2010: 0,
        damage_hq100_status: "calculated_positive", damage_hq100_eur2010: 13573.35 };
      var p = api.nativePayload("ACT_A", values);
      equal(p.footprint_area_m2, 321.75); equal(api.money(p.hq10), "€0.00");
      equal(api.nativePayload("ACT_A", values, false).footprint_area_m2, null);
      equal(api.nativePayload("ACT_B", values), null);
      values.damage_currency = "USD"; equal(api.nativePayload("ACT_A", values), null);
    });
    await test("late response cannot overwrite newly selected building", async function () {
      var resolveA;
      var s = setup("ACT_A", { fetch: function (url) {
        if (url.indexOf("ACT_A") >= 0) return new Promise(function (resolve) { resolveA = resolve; });
        return Promise.resolve({ ok: true, json: function () { return Promise.resolve(payload("ACT_B", 42, 84)); } });
      } });
      s.controller.tick(); await flush();
      var b = feature("ACT_B"); b._catalogItem = s.layer;
      s.terria.selectedFeature = b; s.terria.pickedFeatures.features = [b]; s.controller.tick(); await flush();
      equal(b.values.fc_popup_hq10_damage, "€42.00");
      resolveA({ ok: true, json: function () { return Promise.resolve(payload("ACT_A", 999999, 999999)); } });
      await flush(); equal(b.values.fc_popup_hq10_damage, "€42.00"); equal(b.values.fc_popup_id, "ACT_B");
    });
    await test("restore same original ID after map workbench replacement", async function () {
      var s = setup("ACT_A"); s.controller.tick(); await flush(); s.controller.navigation();
      var next = feature("ACT_A", "building-1234"), item = layer([next]);
      s.terria.workbench.items = [item]; s.terria.selectedFeature = undefined; s.terria.pickedFeatures = undefined;
      s.map("hq100"); s.controller.tick();
      assert(s.terria.selectedFeature === next); equal(next.values.fc_popup_id, "ACT_A");
      assert(next.values.fc_popup_hq100_show && !next.values.fc_popup_hq10_show);
    });
    await test("wait for asynchronous init instead of consuming selection on old layer", async function () {
      var s = setup("ACT_A"); s.controller.tick(); await flush(); s.controller.navigation();
      // The first hashchange tick still sees the previous workbench.
      s.controller.tick();
      var next = feature("ACT_A", "building-100"), item = layer([next]);
      s.terria.workbench.items = [item]; s.terria.selectedFeature = undefined; s.terria.pickedFeatures = undefined;
      s.controller.tick(); assert(s.terria.selectedFeature === next);
    });
    await test("authoritative missing lookup remains N/A even if a layer has costs", async function () {
      var missing = payload("ACT_A", 999, 999); missing.found = false;
      var s = setup("ACT_A", { fetch: function () { return Promise.resolve({ ok: false, status: 404,
        json: function () { return Promise.resolve(missing); } }); } });
      Object.assign(s.feature.values, { damage_currency: "EUR", damage_price_year: 2010,
        damage_estimates_provisional: true, accepted_provisional_classification: true,
        damage_hq10_status: "calculated_positive", damage_hq100_status: "calculated_positive",
        damage_hq10_eur2010: 999, damage_hq100_eur2010: 999 });
      s.controller.tick(); await flush(); equal(s.feature.values.fc_popup_hq10_damage, "N/A");
    });
    await test("do not reopen a deliberately closed popup", async function () {
      var viewState = { featureInfoPanelIsVisible: true }, s = setup("ACT_A", { viewState: viewState });
      s.controller.tick(); await flush(); s.controller.navigation();
      viewState.featureInfoPanelIsVisible = false;
      s.terria.selectedFeature = undefined; s.terria.pickedFeatures = undefined;
      s.controller.tick(); equal(s.terria.selectedFeature, undefined);
    });
    await test("map checkbox detection distinguishes HQ100 from HQ10", function () {
      var hq10 = { checked: false }, hq100 = { checked: true };
      var doc = { getElementById: function (id) { return id.endsWith("hq100") ? hq100 : hq10; } };
      equal(api.mapScenario(doc, {}), "hq100"); hq100.checked = false; hq10.checked = true;
      equal(api.mapScenario(doc, {}), "hq10");
    });
    await test("non-building selection does not get a template or cost request", async function () {
      var s = setup("ACT_A"); delete s.feature.values.buildingid;
      s.controller.tick(); await flush(); equal(s.requested.length, 0);
      equal(s.layer.featureInfoTemplate.template, "<p>{{original_field}}</p>");
    });
    var message = count + " popup checks passed";
    if (typeof print === "function") print(message); else console.log(message);
  }
  run().catch(function (error) {
    if (typeof print === "function") { print(String(error.stack || error)); quit(1); }
    else { console.error(error); process.exitCode = 1; }
  });
})();
