"use strict";

const upload = document.querySelector("[data-upload]");
const validateButton = document.querySelector("[data-validate]");
const validation = document.querySelector("[data-validation]");
const confirmation = document.querySelector("[data-confirmation]");
const confirmationBox = document.querySelector("[data-confirm]");
const flashButton = document.querySelector("[data-flash]");
const preflight = document.querySelector("[data-preflight]");
let artifactId = null;

function request(path, options = {}) {
  const headers = { ...(options.headers || {}), "X-Requested-With": "XMLHttpRequest" };
  return fetch(`./${path}`, {
    ...options,
    credentials: "same-origin",
    headers,
  }).then(async (response) => {
    const payload = await response.json().catch(() => ({ error: "The add-on returned an invalid response." }));
    if (!response.ok) throw new Error(payload.error || "The request failed.");
    return payload;
  });
}

function setText(selector, value) { document.querySelector(selector).textContent = value || "Unknown"; }
function describeState(state) {
  return { application: "CUL application ready", bootloader: "CUL DFU recovery ready", unavailable: "CUL unavailable" }[state] || "CUL state unknown";
}

async function refresh() {
  try {
    const status = await request("api/status", { headers: {} });
    const device = status.device || {};
    setText("[data-device-state]", describeState(device.state));
    setText("[data-device-message]", device.message);
    setText("[data-topology]", device.topology);
    setText("[data-version]", device.last_verified_version);
    updateOperation(status.operation || {});
  } catch (error) {
    setText("[data-device-state]", "Status unavailable");
    setText("[data-device-message]", error.message);
  }
}

function updateOperation(operation) {
  const progress = Number.isInteger(operation.progress) ? operation.progress : 0;
  setText("[data-operation-title]", operation.status || "Ready");
  setText("[data-operation-message]", operation.message || "No flash operation is running.");
  const bar = document.querySelector("[data-progress]");
  bar.style.width = `${Math.max(0, Math.min(100, progress))}%`;
  bar.parentElement.setAttribute("aria-valuenow", String(progress));
  const error = document.querySelector("[data-operation-error]");
  error.hidden = !operation.error;
  error.textContent = operation.error || "";
}

upload.addEventListener("change", () => {
  artifactId = null;
  confirmation.hidden = true;
  confirmationBox.checked = false;
  flashButton.disabled = true;
  validation.hidden = true;
  validateButton.disabled = !upload.files || upload.files.length !== 1;
});

confirmationBox.addEventListener("change", () => { flashButton.disabled = !confirmationBox.checked || !artifactId; });

validateButton.addEventListener("click", async () => {
  const file = upload.files && upload.files[0];
  if (!file) return;
  validateButton.disabled = true;
  validation.hidden = false;
  validation.textContent = "Validating Intel HEX structure and CUL USB target...";
  try {
    const payload = await request("api/validate-upload", {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: file,
    });
    artifactId = payload.artifact_id;
    const firmware = payload.firmware || {};
    const target = payload.preflight || {};
    validation.textContent = `Validated ${firmware.application_bytes || 0} application bytes, SHA-256 ${firmware.sha256 || "unknown"}.`;
    preflight.textContent = target.message || "CUL target preflight completed.";
    confirmation.hidden = false;
  } catch (error) {
    artifactId = null;
    validation.textContent = error.message;
  } finally {
    validateButton.disabled = false;
  }
});

flashButton.addEventListener("click", async () => {
  if (!artifactId || !confirmationBox.checked) return;
  flashButton.disabled = true;
  try {
    await request("api/flash", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ artifact_id: artifactId, confirm: true }),
    });
    artifactId = null;
    confirmation.hidden = true;
    validation.hidden = true;
  } catch (error) {
    document.querySelector("[data-operation-error]").textContent = error.message;
    document.querySelector("[data-operation-error]").hidden = false;
    flashButton.disabled = !confirmationBox.checked || !artifactId;
  }
});

refresh();
window.setInterval(refresh, 2000);
