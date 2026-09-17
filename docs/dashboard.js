import { createDashboardClient } from "/dashboard-client.mjs?v=8";
import { createDemoClient } from "/dashboard-demo.mjs?v=8";
import { createDashboardUi } from "/dashboard-ui.mjs?v=8";

const settingsProjectInput = document.querySelector("#settings-project-id");
const settingsWorkflowInput = document.querySelector("#settings-workflow-id");
const settingsSchedulerEnabled = document.querySelector("#settings-scheduler-enabled");
const settingsMaxWorkers = document.querySelector("#settings-max-workers");
const settingsPollInterval = document.querySelector("#settings-poll-interval");
const schedulerSave = document.querySelector("#scheduler-save");
const schedulerFeedback = document.querySelector("#scheduler-feedback");
const settingsForm = document.querySelector("#settings-form");
const settingsFeedback = document.querySelector("#settings-feedback");
const workflowSelector = document.querySelector("#workflow-select");
const workflowCreateForm = document.querySelector("#workflow-create-form");
const workflowCreateId = document.querySelector("#workflow-create-id");
const workflowCreateFeedback = document.querySelector("#workflow-create-feedback");
const statusOutput = document.querySelector("#state-status");
const connectionState = document.querySelector("#connection-state");
const projectContext = document.querySelector("#project-context");
const pageTitle = document.querySelector("#page-title");
const welcome = document.querySelector("#welcome");
const mission = document.querySelector("#mission");
const refreshButton = document.querySelector("#refresh");
const demoLink = document.querySelector("#demo-link");
const welcomeDemo = document.querySelector("#welcome-demo");
const welcomeSettings = document.querySelector("#welcome-settings");
const params = new URLSearchParams(window.location.search);
const demo = params.get("demo") === "true";
const pageTitles = {
  mission: "Mission control",
  queue: "Queue",
  agents: "Agents",
  deliveries: "Deliveries",
  "workflow-graphs": "Workflow graphs",
  projects: "Projects",
  evidence: "Evidence log",
  settings: "Settings",
};

function normalizePage(value) {
  const page = String(value || "").replace(/^#/, "");
  return Object.hasOwn(pageTitles, page) ? page : "mission";
}

settingsProjectInput.value = demo ? "demo:1" : params.get("project") || "";
let initialWorkflow = params.get("workflow_id") || (demo ? "demo-flow" : "");
settingsWorkflowInput.value = initialWorkflow;
let archivedMode = params.get("archived") === "true";
let schedulerSettingsDirty = false;

function setConnectionState(state, label) {
  if (!connectionState) return;
  connectionState.dataset.state = state;
  connectionState.title = label;
  connectionState.setAttribute("aria-label", label);
}

function setStatus(message, kind = "") {
  if (message === "Loading live state...") {
    setConnectionState("checking", "Checking connection");
    statusOutput.className = "status";
    statusOutput.textContent = "";
    return;
  }
  if (message.startsWith("Updated ")) {
    setConnectionState("connected", "Connected");
    statusOutput.className = "status";
    statusOutput.textContent = "";
    return;
  }
  statusOutput.textContent = message;
  statusOutput.className = `status ${kind}`;
  if (message.startsWith("Live state failed")) {
    setConnectionState("error", "Connection failed");
  }
}

function setSettingsFeedback(message, kind = "") {
  settingsFeedback.textContent = message;
  settingsFeedback.className = `settings-status ${kind}`;
}

function setSchedulerFeedback(message, kind = "") {
  schedulerFeedback.textContent = message;
  schedulerFeedback.className = `settings-status ${kind}`;
}

function setWorkflowCreateFeedback(message, kind = "") {
  workflowCreateFeedback.textContent = message;
  workflowCreateFeedback.className = `settings-status ${kind}`;
}

function workflowDraft(workflowId) {
  const result = {
    outcome: "pass",
    evidence: {},
    artifact_refs: [],
    question: null,
    required_action: null,
    validation_reason: null,
    answer: null,
  };
  return {
    workflow_id: workflowId,
    revision: 1,
    schema_version: 1,
    nodes: [
      { node_id: "start", kind: "prompt", reference: { reference_id: "prompt/start" } },
      { node_id: "work", kind: "skill", reference: { reference_id: "skill/work" } },
    ],
    edges: [{ source: "start", target: "work", condition: "pass" }],
    fixtures: { happy: { start: result, work: result } },
  };
}

function showApp() {
  welcome.hidden = true;
  mission.hidden = false;
}

function showWelcome() {
  welcome.hidden = false;
  mission.hidden = true;
}

function updateUrl(project, workflow = settingsWorkflowInput.value.trim(), keepDemo = false) {
  const next = new URL(window.location.href);
  if (keepDemo) next.searchParams.set("demo", "true");
  else next.searchParams.delete("demo");
  next.searchParams.set("project", project);
  if (workflow) next.searchParams.set("workflow_id", workflow);
  else next.searchParams.delete("workflow_id");
  if (archivedMode) next.searchParams.set("archived", "true");
  else next.searchParams.delete("archived");
  window.history.replaceState({}, "", next);
}

let client;
const ui = createDashboardUi({
  document,
  projectSwitcher: document.querySelector("#project-switcher"),
  refreshAge: document.querySelector("#refresh-age"),
  metrics: document.querySelector("#metrics"),
  attention: document.querySelector("#attention"),
  filters: document.querySelector("#filters"),
  queueTable: document.querySelector("#queue-table"),
  agentsOutput: document.querySelector("#agents-output"),
  activityOutput: document.querySelector("#activity-output"),
  deliveriesOutput: document.querySelector("#deliveries-output"),
  graphOutput: document.querySelector("#graph-output"),
  workflowSelector,
  settingsWorkflowInput,
  projectsOutput: document.querySelector("#projects-output"),
  schedulerCard: document.querySelector("#scheduler-card"),
  detailsPane: document.querySelector("#details-pane"),
  detailsOutput: document.querySelector("#details-output"),
  runAutonomous: (payload) => client.runAutonomous(payload),
  onQueueArchive: (archived) => {
    archivedMode = archived;
    const project = settingsProjectInput.value.trim();
    if (project) updateUrl(project, settingsWorkflowInput.value.trim(), demo);
    void client.refresh();
  },
  initialFilter: archivedMode ? "archived" : "all",
  demo,
  runAction: (payload) => client.runAction(payload),
});

function setPage(page, updateHash = true) {
  const nextPage = normalizePage(page);
  if (updateHash && window.location.hash !== `#${nextPage}`) {
    window.history.replaceState({}, "", `${window.location.pathname}${window.location.search}#${nextPage}`);
  }
  ui.setPage(nextPage);
  pageTitle.textContent = pageTitles[nextPage];
  document.querySelectorAll("[data-page]").forEach((node) => {
    const active = node.dataset.page === nextPage;
    node.classList.toggle("active", active);
    if (active) node.setAttribute("aria-current", "page");
    else node.removeAttribute("aria-current");
  });
  if (nextPage === "settings" || client) showApp();
  else showWelcome();
}

function handleState(state) {
  showApp();
  const project = state.project_id || settingsProjectInput.value.trim();
  projectContext.textContent = state.name ? `${state.name} · ${project}` : project || "No project selected";
  const scheduler = state.scheduler;
  if (scheduler && !schedulerSettingsDirty) {
    settingsSchedulerEnabled.value = String(scheduler.enabled === true);
    settingsMaxWorkers.value = String(scheduler.max_concurrency || 1);
    settingsPollInterval.value = String(scheduler.poll_interval_seconds || 600);
  }
  ui.render(state);
  if (initialWorkflow && settingsWorkflowInput.value === initialWorkflow) initialWorkflow = "";
}

function toggleBusy(disabled) {
  refreshButton.disabled = disabled;
  settingsProjectInput.disabled = disabled;
  settingsWorkflowInput.disabled = disabled;
  settingsForm.querySelector("#settings-save").disabled = disabled;
  settingsSchedulerEnabled.disabled = disabled;
  settingsMaxWorkers.disabled = disabled;
  settingsPollInterval.disabled = disabled;
  schedulerSave.disabled = disabled;
}

if (demo) {
  client = createDemoClient({
    archived: () => archivedMode,
    workflowId: () => settingsWorkflowInput.value.trim() || initialWorkflow,
    onState: handleState,
    onStatus: setStatus,
      onBusy: toggleBusy,
  });
  demoLink.textContent = "Exit demo";
  demoLink.href = "/dashboard";
  setStatus("Demo mode uses placeholder systems. No external changes are made.");
} else {
  client = createDashboardClient({
    fetcher: window.fetch.bind(window),
    projectId: () => settingsProjectInput.value,
    workflowId: () => settingsWorkflowInput.value || initialWorkflow,
    archived: () => archivedMode,
    onState: handleState,
    onStatus: setStatus,
    onBusy: toggleBusy,
  });
}

function openProject(navigateToMission = true) {
  const project = settingsProjectInput.value.trim();
  const workflow = settingsWorkflowInput.value.trim() || initialWorkflow;
  if (!project) {
    setSettingsFeedback("Enter a project ID such as owner:123.", "failure");
    setStatus("Choose a project in Settings before opening mission control.", "failure");
    setPage("settings");
    return;
  }
  if (demo && project !== "demo:1") {
    const next = new URL(window.location.href);
    next.searchParams.delete("demo");
    next.searchParams.set("project", project);
    if (workflow) next.searchParams.set("workflow_id", workflow);
    next.hash = "#mission";
    window.location.href = next;
    return;
  }
  updateUrl(project, workflow, demo);
  setSettingsFeedback("Saved for this dashboard URL.", "success");
  if (navigateToMission) setPage("mission");
  void client.refresh();
}

settingsForm.addEventListener("submit", (event) => {
  event.preventDefault();
  openProject();
});
refreshButton.addEventListener("click", () => void client.refresh());
for (const input of [settingsSchedulerEnabled, settingsMaxWorkers, settingsPollInterval]) {
  input.addEventListener("input", () => { schedulerSettingsDirty = true; });
  input.addEventListener("change", () => { schedulerSettingsDirty = true; });
}
schedulerSave.addEventListener("click", () => {
  const project = settingsProjectInput.value.trim();
  const maxConcurrency = Number(settingsMaxWorkers.value);
  const pollInterval = Number(settingsPollInterval.value);
  if (!project) {
    setSchedulerFeedback("Choose a project before configuring workers.", "failure");
    setPage("settings");
    return;
  }
  if (!Number.isInteger(maxConcurrency) || maxConcurrency < 1 || !Number.isFinite(pollInterval) || pollInterval <= 0) {
    setSchedulerFeedback("Worker capacity and poll interval must be positive numbers.", "failure");
    return;
  }
  void client.configureScheduler({
    enabled: settingsSchedulerEnabled.value === "true",
    max_concurrency: maxConcurrency,
    poll_interval_seconds: pollInterval,
  }).then((result) => {
    if (result && !result.error) {
      schedulerSettingsDirty = false;
      setSchedulerFeedback("Scheduler settings applied.", "success");
    } else if (result?.error) {
      setSchedulerFeedback(`Scheduler settings failed: ${result.error}`, "failure");
    } else {
      setSchedulerFeedback("Scheduler settings failed.", "failure");
    }
  });
});
workflowCreateForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const workflowId = workflowCreateId.value.trim();
  const project = settingsProjectInput.value.trim();
  if (!project) {
    setWorkflowCreateFeedback("Choose a project in Settings first.", "failure");
    setPage("settings");
    return;
  }
  if (!workflowCreateForm.checkValidity()) {
    setWorkflowCreateFeedback("Use a lowercase workflow ID such as delivery-flow.", "failure");
    workflowCreateId.focus();
    return;
  }
  const draft = workflowDraft(workflowId);
  settingsWorkflowInput.value = workflowId;
  updateUrl(project, workflowId, demo);
  void client.runAction({
    action: "graph_evaluate",
    workflow_id: workflowId,
    candidate: draft,
    fixtures: draft.fixtures,
  }).then((result) => {
    if (!result || result.error) {
      setWorkflowCreateFeedback(`The workflow draft could not be created${result?.error ? `: ${result.error}` : "."}`, "failure");
      return;
    }
    setWorkflowCreateFeedback("Workflow draft created and evaluated.", "success");
    workflowCreateId.value = "";
  });
});
workflowSelector.addEventListener("change", () => {
  settingsWorkflowInput.value = workflowSelector.value;
  const project = settingsProjectInput.value.trim();
  if (!project) {
    setPage("settings");
    return;
  }
  updateUrl(project, settingsWorkflowInput.value, demo);
  void client.refresh();
});
welcomeDemo.addEventListener("click", () => {
  window.location.href = "/dashboard?demo=true#mission";
});
welcomeSettings.addEventListener("click", () => setPage("settings"));
document.querySelectorAll("[data-page]").forEach((node) => {
  node.addEventListener("click", () => setPage(node.dataset.page));
});
window.addEventListener("hashchange", () => setPage(window.location.hash, false));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") ui.closeInspector();
});

setPage(window.location.hash || "mission", false);
if (demo || settingsProjectInput.value.trim()) openProject(false);
else showWelcome();
if (!demo) window.setInterval(() => void client.refresh(), 15000);
