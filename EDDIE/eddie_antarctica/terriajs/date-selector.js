(function () {
  "use strict";

  const API_BASE = "$BACKEND_URL";

  // Use versioned keys so old cached messages such as
  // "Refreshing temporal layers..." are not restored.
  const DATE_KEY = "dt-selected-date-v2";
  const MESSAGE_KEY = "dt-load-message-v2";

  // Remove obsolete values from the previous implementation.
  sessionStorage.removeItem("dt-selected-date");
  sessionStorage.removeItem("dt-load-message");

  // =========================================================
  // STYLE
  // =========================================================

  const style = document.createElement("style");

  style.textContent = `
    #dt-date-box {
      position: fixed;
      top: 75px;
      right: 20px;
      z-index: 99999;
      width: 285px;
      padding: 14px;
      background: rgba(25, 34, 45, 0.95);
      color: white;
      border-radius: 6px;
      font-family: Arial, sans-serif;
      box-shadow: 0 2px 12px rgba(0, 0, 0, 0.35);
    }

    #dt-date-box h3 {
      margin: 0 0 10px 0;
      font-size: 15px;
    }

    #dt-date-row {
      display: flex;
      gap: 8px;
    }

    #dt-date-input {
      flex: 1;
      padding: 6px;
    }

    #dt-load-btn {
      padding: 6px 12px;
      cursor: pointer;
    }

    #dt-load-btn:disabled {
      opacity: 0.6;
      cursor: wait;
    }

    #dt-status {
      margin-top: 10px;
      font-size: 12px;
      line-height: 1.5;
    }

    .dt-ok {
      color: #7CFC98;
    }

    .dt-warn {
      color: #FFD166;
    }

    .dt-error {
      color: #FF7B7B;
    }

    .dt-processing {
      opacity: 0.85;
    }
  `;

  document.head.appendChild(style);

  // =========================================================
  // UI
  // =========================================================

  const box = document.createElement("div");
  box.id = "dt-date-box";

  box.innerHTML = `
    <h3>Digital Twin date</h3>

    <div id="dt-date-row">
      <input
        id="dt-date-input"
        type="date"
      >

      <button id="dt-load-btn">
        Load
      </button>
    </div>

    <div id="dt-status">
      Select a date and load the dynamic data.
    </div>
  `;

  document.body.appendChild(box);

  const input =
    document.getElementById("dt-date-input");

  const button =
    document.getElementById("dt-load-btn");

  const status =
    document.getElementById("dt-status");

  // =========================================================
  // RESTORE LAST SUCCESSFUL STATE AFTER PAGE RELOAD
  // =========================================================

  const previousDate =
    sessionStorage.getItem(DATE_KEY);

  const previousMessage =
    sessionStorage.getItem(MESSAGE_KEY);

  if (previousDate) {
    input.value = previousDate;
  }

  if (previousMessage) {
    status.innerHTML = previousMessage;
  }

  // =========================================================
  // HELPERS
  // =========================================================

  function sleep(ms) {
    return new Promise(
      resolve => setTimeout(resolve, ms)
    );
  }

  async function getTask(taskId) {
    const response = await fetch(
      `${API_BASE}/digital-twin-task-status/${taskId}`
    );

    if (!response.ok) {
      throw new Error(
        `Task status request failed (${response.status})`
      );
    }

    return response.json();
  }

  async function waitForTask(taskId) {
    // 120 x 1.5 s = maximum 3 minutes
    for (let i = 0; i < 120; i++) {
      const result = await getTask(taskId);

      if (
        result.status === "finished" ||
        result.status === "error"
      ) {
        return result;
      }

      await sleep(1500);
    }

    throw new Error(
      "Processing timeout."
    );
  }

  // =========================================================
  // PRECIPITATION MESSAGE
  // =========================================================

  function precipitationMessage(task) {
    if (
      task.status === "finished" &&
      task.result
    ) {
      return `
        <span class="dt-ok">
          ✓ Precipitation loaded
        </span>
      `;
    }

    const message =
      (
        task.message ||
        task.result?.message ||
        ""
      ).toLowerCase();

    if (
      message.includes("no usable") ||
      message.includes("no precipitation") ||
      message.includes("no data")
    ) {
      return `
        <span class="dt-warn">
          ⚠ No precipitation data for this date
        </span>
      `;
    }

    return `
      <span class="dt-error">
        ✗ Precipitation could not be loaded
      </span>
    `;
  }

  // =========================================================
  // WATER LEVEL MESSAGE
  // =========================================================

  function waterMessage(task) {
    if (
      task.status === "finished" &&
      task.result
    ) {
      if (
        task.result.status === "no_data" ||
        task.result.status === "no_stations"
      ) {
        return `
          <span class="dt-warn">
            ⚠ No water-level data for this date
          </span>
        `;
      }

      if (
        task.result.status === "success"
      ) {
        return `
          <span class="dt-ok">
            ✓ Water level loaded
          </span>
        `;
      }
    }

    const message =
      (
        task.message ||
        task.result?.message ||
        ""
      ).toLowerCase();

    if (
      message.includes("no water") ||
      message.includes("no data") ||
      message.includes("unavailable")
    ) {
      return `
        <span class="dt-warn">
          ⚠ No water-level data for this date
        </span>
      `;
    }

    return `
      <span class="dt-error">
        ✗ Water level could not be loaded
      </span>
    `;
  }

  // =========================================================
  // LOAD SELECTED DATE
  // =========================================================

  button.addEventListener(
    "click",
    async () => {

      const selectedDate =
        input.value;

      if (!selectedDate) {
        status.innerHTML = `
          <span class="dt-warn">
            Please select a date.
          </span>
        `;

        return;
      }

      button.disabled = true;

      status.innerHTML = `
        <strong>${selectedDate}</strong><br>

        <span class="dt-processing">
          Precipitation: processing...
        </span><br>

        <span class="dt-processing">
          Water level: processing...
        </span>
      `;

      try {

        // -----------------------------------------------------
        // Start both Digital Twin pipelines
        // -----------------------------------------------------

        const response = await fetch(
          `${API_BASE}/load-digital-twin-date`,
          {
            method: "POST",

            headers: {
              "Content-Type": "application/json"
            },

            body: JSON.stringify({
              date: selectedDate
            })
          }
        );

        const data =
          await response.json();

        if (!response.ok) {
          throw new Error(
            data.message ||
            "Could not start data loading."
          );
        }

        const precipitationId =
          data.precipitation.task_id;

        const waterId =
          data.water_level.task_id;

        // -----------------------------------------------------
        // Wait for both pipelines independently
        // -----------------------------------------------------

        const results =
          await Promise.all([
            waitForTask(precipitationId),
            waitForTask(waterId)
          ]);

        const precipitation =
          results[0];

        const water =
          results[1];

        // -----------------------------------------------------
        // Final message to KEEP after reload
        // -----------------------------------------------------

        const readyMessage = `
          <strong>${selectedDate}</strong><br>

          ${precipitationMessage(precipitation)}
          <br>

          ${waterMessage(water)}
          <br>

          <span class="dt-ok">
            ✓ Data ready
          </span>
        `;

        // -----------------------------------------------------
        // Temporary message before reload only
        // -----------------------------------------------------

        status.innerHTML = `
          ${readyMessage}

          <br>

          <span class="dt-processing">
            Refreshing temporal layers...
          </span>
        `;

        // -----------------------------------------------------
        // Save ONLY the stable message.
        // "Refreshing..." is deliberately NOT stored.
        // -----------------------------------------------------

        sessionStorage.setItem(
          DATE_KEY,
          selectedDate
        );

        sessionStorage.setItem(
          MESSAGE_KEY,
          readyMessage
        );

        // -----------------------------------------------------
        // Give GeoServer a moment to expose the new TIME domain.
        // -----------------------------------------------------

        await sleep(2500);

        // -----------------------------------------------------
        // Tell Terria to open the selected Digital Twin date.
        // -----------------------------------------------------

        const precipitationAvailable =
  	  precipitation.status === "finished" &&
          precipitation.result &&
          Number(precipitation.result.surface_count || 0) > 0;

        const waterAvailable =
          water.status === "finished" &&
          water.result &&
          water.result.status === "success";

        const dateInitUrl =
  	  `${API_BASE}/terria-date-init/${selectedDate}/` +
          `${precipitationAvailable ? "1" : "0"}/` +
          `${waterAvailable ? "1" : "0"}.json`;

        window.location.hash =
          dateInitUrl;

        // -----------------------------------------------------
        // Reload Terria so:
        // 1. WMS GetCapabilities is refreshed
        // 2. new timestamps are discovered
        // 3. selected date becomes current time
        // -----------------------------------------------------

        window.location.reload();

      } catch (error) {

        status.innerHTML = `
          <span class="dt-error">
            ✗ ${error.message}
          </span>
        `;

        button.disabled = false;
      }
    }
  );
})();