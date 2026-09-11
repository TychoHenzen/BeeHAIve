import { createDashboardClient } from "/dashboard-client.mjs";
import { createDashboardView } from "./dashboard-view.mjs";

const projectInput = document.querySelector("#project-id");
const apiKeyInput = document.querySelector("#api-key");
const statusOutput = document.querySelector("#state-status");
const summaryOutput = document.querySelector("#summary");
const dashboardOutput = document.querySelector("#dashboard");
const actionLog = document.querySelector("#action-log");
const actionsOutput = document.querySelector("#actions");
const refreshButton = document.querySelector("#refresh");
const startButton = document.querySelector("#start");
const archivedInput = document.querySelector("#archived-view");

const params = new URLSearchParams(window.location.search);
projectInput.value = params.get("project") || "";
archivedInput.checked = params.get("archived") === "true";
apiKeyInput.value = window.localStorage.getItem("beehaiive-api-key") || "";

function projectId() {
  return projectInput.value.trim();
}

function setStatus(message, kind = "") {
  statusOutput.textContent = message;
  statusOutput.className = `status ${kind}`;
}

function toggleControls(disabled) {
  refreshButton.disabled = disabled;
  startButton.disabled = disabled;
  archivedInput.disabled = disabled;
}

let client;
const view = createDashboardView({
  document,
  summaryOutput,
  dashboardOutput,
  actionLog,
  actionsOutput,
  runAction: (payload) => client.runAction(payload),
});

client = createDashboardClient({
  fetcher: window.fetch.bind(window),
  projectId,
  archived: () => archivedInput.checked,
  apiKey: () => apiKeyInput.value.trim(),
  saveApiKey: (value) => window.localStorage.setItem("beehaiive-api-key", value),
  onState: view.render,
  onStatus: setStatus,
  onBusy: toggleControls,
});

function refresh() {
  return client.refresh();
}

refreshButton.addEventListener("click", refresh);
startButton.addEventListener("click", () => client.runAction({ action: "start" }));
projectInput.addEventListener("change", refresh);
archivedInput.addEventListener("change", refresh);
window.setInterval(refresh, 5000);
if (projectId()) refresh();
