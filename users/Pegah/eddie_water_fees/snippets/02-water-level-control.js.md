# Snippet 2 — a Run button in the map panel (required to click it)

`EDDIE/eddie_antarctica/terriajs/water-level-control.js`

Nothing has to be added for the *layer*: the two models write columns on
`building_population`, and the view `building_population_4326` behind
`input_layers:building_population_4326` is recreated by the task, so the existing
**Water fees (model result)** tick under *River* shows the new columns on the same
buildings. Only the button is missing.

Paste the block below **immediately after** the existing water-fee panel block, i.e. after
these two lines (around line 2504 in the file as it is now):

```js
    fees.appendChild(element("div", "wl-note",
      "Tick Water fees in the River category to show the result on the map. The run takes about a minute."));
```

```js
    // Scenario 1 (15-minute gauge readings) and scenario 2 (seasonal rain), from the
    // eddie_water_fees plugin. It writes columns on building_population, so the result is
    // drawn by the same "Water fees (model result)" layer: tick it under River.
    var fees15 = section(demos.fees, "15-minute tariff and rain");
    var fees15Table = element("table");

    function fees15Text(id, placeholder) {
      var input = document.createElement("input");
      input.type = "text";
      input.id = id;
      input.placeholder = placeholder;
      return input;
    }

    var gaugeFrom = fees15Text("water-level-control-fees15-from", "empty = 7 days before the end");
    settingRow(fees15Table, "Gauges from (UTC)", gaugeFrom, "");
    var gaugeTo = fees15Text("water-level-control-fees15-to", "empty = now");
    settingRow(fees15Table, "Gauges to (UTC)", gaugeTo, "");

    var fee15Share = rangeSlider("water-level-control-fees15-percentage", 0, 1, 0.01, 0.1, percent, function () {});
    settingRow(fees15Table, "Extra share", fee15Share.input, fee15Share.value);

    var fee15Price = document.createElement("input");
    fee15Price.type = "number";
    fee15Price.min = "0";
    fee15Price.max = "1000";
    fee15Price.step = "0.5";
    fee15Price.value = "7";
    settingRow(fees15Table, "Tariff", fee15Price, "€/m³");

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
          fees15Note.textContent = "Last run: " + euro.format(data.buildings) + " buildings (" +
            euro.format(data.fee_buildings) + " non-residential); mean tariff €" +
            euro.format(data.tariff_mean_eur) + " over " + data.intervals + " intervals (" +
            data.gauge_quality + "); rain " + data.rain_season + " " + data.rain_mm + " mm -> " +
            euro.format(data.rain_volume_m3) + " m³ on the roofs, €" + euro.format(data.rainwater_cost_eur) + ".";
        })
        .catch(function () { fees15Note.textContent = "Could not reach the backend."; });
    }

    fees15.appendChild(button("Run 15-minute tariff + rain", function () {
      fees15Note.textContent = "Running the model...";
      fetch(BACKEND_URL + "/water-fees/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          percentage: Number(fee15Share.input.value),
          unit_price: Number(fee15Price.value),
          rainwater_unit_price: Number(rainPrice.value),
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
```

Everything it uses (`section`, `element`, `settingRow`, `rangeSlider`, `checkbox`, `button`,
`waitForTask`, `reloadView`, `mapLayers`, `BACKEND_URL`, `percent`, `demos`, `fetch`) already
exists in that file, so the snippet needs no other change.

Terria files are baked into the image: rebuild it **and** bump the version so browsers fetch
the new script — in `terriajs/entrypoint.sh` the two `water-level-control.js?v=23` become
`?v=24` (lines 47 and 51), then

```powershell
cd EDDIE/eddie_antarctica
docker compose up -d --build --no-deps terria_map
```

Then reload <http://localhost:3001>, open **Water-fee model** in the central panel, and the
section *15-minute tariff and rain* is at the bottom of it.
