const board = document.querySelector("#board");
const statusText = document.querySelector("#snapshot-status");
const workflowList = document.querySelector("#workflow-list");
const workflowEditor = document.querySelector("#workflow-editor");
const workflowStatus = document.querySelector("#workflow-status");
const workflowName = document.querySelector("#workflow-name");
const workflowPrompt = document.querySelector("#workflow-prompt");
const workflowParameters = document.querySelector("#workflow-parameters");
const workflowCanvas = document.querySelector("#workflow-canvas");
const workflowJson = document.querySelector("#workflow-json");
const workflowErrors = document.querySelector("#workflow-errors");
const workflowSave = document.querySelector("#workflow-save");
let currentWorkflowId = null;
let currentDefinition = null;

function text(value) {
  return value == null ? "" : String(value);
}

function render(snapshot) {
  board.replaceChildren();
  for (const column of snapshot.columns ?? []) {
    const columnElement = document.createElement("section");
    columnElement.className = "column";
    const heading = document.createElement("h3");
    heading.textContent = text(column.status);
    columnElement.append(heading);
    for (const item of column.items ?? []) {
      const card = document.createElement("article");
      card.className = "card";
      const icon = document.createElement("span");
      icon.className = "type-icon";
      icon.setAttribute("aria-label", text(item.type));
      icon.textContent = { Issue: "●", PullRequest: "↗", DraftIssue: "✎" }[item.type] ?? "•";
      const meta = document.createElement("p");
      meta.className = "card-meta";
      meta.append(icon, document.createTextNode(` ${text(item.type)} · ${text(item.repository)}#${text(item.number)}`));
      const title = document.createElement(item.url ? "a" : "h4");
      title.textContent = text(item.title);
      if (item.url) {
        title.href = item.url;
        title.target = "_blank";
        title.rel = "noreferrer";
      }
      const labels = document.createElement("p");
      labels.className = "labels";
      labels.textContent = (item.labels ?? []).join(" · ");
      const holder = document.createElement("p");
      holder.className = "card-holder";
      holder.textContent = "held by agent X";
      card.append(meta, title, labels, holder);
      columnElement.append(card);
    }
    board.append(columnElement);
  }
  const limited = snapshot.rate_limited_until
    ? ` · GitHub rate limited until ${new Date(snapshot.rate_limited_until).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
    : "";
  statusText.textContent = `Snapshot ${text(snapshot.fetched_at) || "not fetched"}${limited}`;
}

async function load(path = "/api/project", method = "GET") {
  statusText.textContent = "Loading the linked Project snapshot…";
  const response = await fetch(path, { method });
  if (!response.ok) {
    throw new Error(`Project request failed: ${response.status}`);
  }
  render(await response.json());
}

async function workflowRequest(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.detail?.errors?.map((item) => item.message).join("; ") || payload.detail || `Workflow request failed: ${response.status}`);
  }
  return payload;
}

function defaultWorkflow() {
  return {
    schema_version: 1,
    name: "Project delivery",
    description: "Wait for a project issue and run a skill.",
    source_prompt: "Wait for an issue in {backlog}, then run {skill}.",
    auto_reset_on_stall: true,
    max_steps_per_pass: 20,
    parameters: [
      { name: "backlog", type: "status", const: true, value: "Backlog" },
      { name: "skill", type: "skill", const: false, value: null },
    ],
    initial: "wait",
    states: [
      { id: "wait", title: "Wait for work", action: "wait_for_work", max_visits: 3, layout: { x: 0, y: 0 } },
      { id: "run", title: "Run skill", action: "run_skill", skill: "{skill}", prompt: "Work the held item.", outcomes: ["done", "blocked"], max_visits: 3, layout: { x: 280, y: 0 } },
      { id: "escalate", title: "Escalate", action: "escalate", max_visits: 3, layout: { x: 560, y: 150 } },
    ],
    transitions: [
      { from: "wait", to: "run", priority: 1, conditions: [{ kind: "item_status_is", value: "{backlog}" }] },
      { from: "run", to: "wait", priority: 1, conditions: [{ kind: "outcome_is", value: "done" }] },
      { from: "run", to: "escalate", priority: 2, conditions: [{ kind: "outcome_is", value: "blocked" }] },
      { from: "escalate", to: "wait", priority: 1, conditions: [{ kind: "always" }] },
    ],
  };
}

function showEditor(definition, workflowId = null) {
  currentWorkflowId = workflowId;
  currentDefinition = structuredClone(definition);
  workflowName.value = text(currentDefinition.name);
  workflowPrompt.value = text(currentDefinition.source_prompt);
  workflowEditor.hidden = false;
  workflowList.hidden = true;
  renderWorkflowEditor();
}

function renderWorkflowEditor() {
  if (!currentDefinition) return;
  workflowJson.value = JSON.stringify(currentDefinition, null, 2);
  workflowParameters.replaceChildren();
  for (const [index, parameter] of (currentDefinition.parameters ?? []).entries()) {
    const row = document.createElement("div");
    row.className = "parameter-row";
    const name = document.createElement("input");
    name.value = text(parameter.name);
    name.placeholder = "parameter name";
    name.addEventListener("change", () => { parameter.name = name.value; validateCurrent(); });
    const type = document.createElement("select");
    for (const option of ["status", "label", "skill", "repository", "item_type", "text", "number", "boolean"]) {
      const item = document.createElement("option");
      item.value = option;
      item.textContent = option;
      item.selected = parameter.type === option;
      type.append(item);
    }
    type.addEventListener("change", () => { parameter.type = type.value; validateCurrent(); });
    const constant = document.createElement("input");
    constant.type = "checkbox";
    constant.checked = parameter.const === true;
    constant.setAttribute("aria-label", "constant parameter");
    constant.addEventListener("change", () => {
      parameter.const = constant.checked;
      parameter.value = constant.checked ? (parameter.value ?? "") : null;
      renderWorkflowEditor();
    });
    const value = document.createElement("input");
    value.value = text(parameter.value);
    value.disabled = !constant.checked;
    value.placeholder = constant.checked ? "constant value" : "assigned at runtime";
    value.addEventListener("change", () => { parameter.value = value.value; validateCurrent(); });
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "Delete";
    remove.addEventListener("click", () => {
      currentDefinition.parameters.splice(index, 1);
      renderWorkflowEditor();
    });
    row.append(name, type, constant, value, remove);
    workflowParameters.append(row);
  }
  workflowCanvas.replaceChildren();
  for (const state of currentDefinition.states ?? []) {
    const card = document.createElement("article");
    card.className = "state-card";
    const title = document.createElement("h3");
    title.textContent = `${state.id === currentDefinition.initial ? "★ " : ""}${text(state.title || state.id)}`;
    const action = document.createElement("p");
    action.textContent = text(state.action);
    const titleInput = document.createElement("input");
    titleInput.value = text(state.title || state.id);
    titleInput.setAttribute("aria-label", `${state.id} title`);
    titleInput.addEventListener("change", () => { state.title = titleInput.value; renderWorkflowEditor(); });
    const actionSelect = document.createElement("select");
    for (const option of ["wait_for_work", "run_skill", "escalate"]) {
      const item = document.createElement("option");
      item.value = option;
      item.textContent = option;
      item.selected = state.action === option;
      actionSelect.append(item);
    }
    actionSelect.addEventListener("change", () => { state.action = actionSelect.value; validateCurrent(); });
    const promptInput = document.createElement("input");
    promptInput.value = text(state.prompt);
    promptInput.placeholder = "state-specific prompt";
    promptInput.setAttribute("aria-label", `${state.id} prompt`);
    promptInput.addEventListener("change", () => { state.prompt = promptInput.value; validateCurrent(); });
    const edit = document.createElement("button");
    edit.type = "button";
    edit.textContent = "Edit JSON";
    edit.addEventListener("click", () => {
      workflowJson.hidden = false;
      document.querySelector("#workflow-advanced").checked = true;
      workflowJson.focus();
    });
    const duplicate = document.createElement("button");
    duplicate.type = "button";
    duplicate.textContent = "Duplicate";
    duplicate.addEventListener("click", () => duplicateState(state.id));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "Delete";
    remove.disabled = state.id === currentDefinition.initial;
    remove.addEventListener("click", () => deleteState(state.id));
    card.append(title, titleInput, action, actionSelect, promptInput, edit, duplicate, remove);
    workflowCanvas.append(card);
  }
  const transitions = document.createElement("div");
  transitions.className = "workflow-transitions";
  for (const [index, transition] of (currentDefinition.transitions ?? []).entries()) {
    const edge = document.createElement("div");
    edge.className = "transition-card";
    edge.textContent = `${text(transition.from)} → ${text(transition.to)} · ${(transition.conditions ?? []).map((condition) => `${condition.kind}${condition.value == null ? "" : `=${condition.value}`}`).join(" & ")}`;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "Delete";
    remove.addEventListener("click", () => {
      currentDefinition.transitions.splice(index, 1);
      renderWorkflowEditor();
    });
    edge.append(remove);
    transitions.append(edge);
  }
  workflowCanvas.append(transitions);
  workflowErrors.textContent = "";
  validateCurrent();
}

async function validateCurrent() {
  if (!currentDefinition) return;
  try {
    const result = await workflowRequest("/api/workflows/validate", {
      method: "POST",
      body: JSON.stringify({ definition: currentDefinition }),
    });
    workflowSave.disabled = !result.valid;
    workflowErrors.textContent = JSON.stringify([...result.errors, ...result.warnings], null, 2);
  } catch (error) {
    workflowSave.disabled = true;
    workflowErrors.textContent = error.message;
  }
}

function duplicateState(stateId) {
  const state = currentDefinition.states.find((candidate) => candidate.id === stateId);
  if (!state) return;
  const copy = structuredClone(state);
  copy.id = `${stateId}_copy`;
  copy.title = `${text(copy.title)} copy`;
  currentDefinition.states.push(copy);
  renderWorkflowEditor();
}

function deleteState(stateId) {
  if (stateId === currentDefinition.initial) return;
  currentDefinition.states = currentDefinition.states.filter((state) => state.id !== stateId);
  currentDefinition.transitions = currentDefinition.transitions.filter((edge) => edge.from !== stateId && edge.to !== stateId);
  renderWorkflowEditor();
}

function addState() {
  const id = `state_${currentDefinition.states.length + 1}`;
  currentDefinition.states.push({
    id,
    title: "New state",
    action: "run_skill",
    skill: "",
    prompt: "",
    outcomes: ["done"],
    max_visits: 3,
  });
  renderWorkflowEditor();
}

function addTransition() {
  const states = currentDefinition.states ?? [];
  if (states.length < 2) return;
  currentDefinition.transitions.push({
    from: states[0].id,
    to: states[1].id,
    priority: currentDefinition.transitions.length + 1,
    conditions: [{ kind: "always" }],
  });
  renderWorkflowEditor();
}

function addParameter() {
  currentDefinition.parameters.push({ name: `parameter_${currentDefinition.parameters.length + 1}`, type: "text", const: false, value: null });
  renderWorkflowEditor();
}

async function loadWorkflows() {
  const payload = await workflowRequest("/api/workflows");
  workflowList.replaceChildren();
  for (const workflow of payload.workflows ?? []) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `${workflow.name} · #${workflow.id}`;
    button.addEventListener("click", async () => {
      const detail = await workflowRequest(`/api/workflows/${workflow.id}`);
      showEditor(detail.latest.definition, workflow.id);
    });
    workflowList.append(button);
  }
}

async function generateWorkflow() {
  workflowStatus.textContent = "Generating with codex exec…";
  const parameters = currentDefinition?.parameters ?? [];
  const job = await workflowRequest("/api/workflows/generate", {
    method: "POST",
    body: JSON.stringify({ name: workflowName.value, prompt: workflowPrompt.value, parameters }),
  });
  let result = await workflowRequest(`/api/workflows/generate/${job.id}`);
  while (result.status === "pending") {
    await new Promise((resolve) => setTimeout(resolve, 500));
    result = await workflowRequest(`/api/workflows/generate/${job.id}`);
  }
  if (result.draft) {
    showEditor(result.draft, currentWorkflowId);
  }
  workflowStatus.textContent = result.status === "succeeded" ? "Generated draft ready to edit." : (result.error || "Generation failed; inspect the validation errors.");
  workflowErrors.textContent = JSON.stringify(result.validation_errors ?? [], null, 2);
}

async function saveWorkflow() {
  const payload = { definition: currentDefinition, source_prompt: workflowPrompt.value, name: workflowName.value };
  const path = currentWorkflowId ? `/api/workflows/${currentWorkflowId}/revisions` : "/api/workflows";
  const result = await workflowRequest(path, { method: "POST", body: JSON.stringify(payload) });
  currentWorkflowId = result.id;
  currentDefinition = result.latest.definition;
  workflowStatus.textContent = `Saved revision ${result.latest.revision}.`;
  await loadWorkflows();
}

document.querySelector("#refresh").addEventListener("click", async () => {
  try {
    await load("/api/project/refresh", "POST");
  } catch (error) {
    statusText.textContent = error.message;
  }
});

document.querySelector("#workflow-new").addEventListener("click", () => showEditor(defaultWorkflow()));
document.querySelector("#workflow-example").addEventListener("click", () => showEditor(defaultWorkflow()));
document.querySelector("#workflow-return").addEventListener("click", async () => {
  workflowEditor.hidden = true;
  workflowList.hidden = false;
  await loadWorkflows();
});
document.querySelector("#workflow-generate").addEventListener("click", () => generateWorkflow().catch((error) => { workflowStatus.textContent = error.message; }));
document.querySelector("#workflow-save").addEventListener("click", () => saveWorkflow().catch((error) => { workflowStatus.textContent = error.message; }));
document.querySelector("#workflow-add-state").addEventListener("click", addState);
document.querySelector("#workflow-add-transition").addEventListener("click", addTransition);
document.querySelector("#workflow-add-parameter").addEventListener("click", addParameter);
document.querySelector("#workflow-help").addEventListener("click", () => {
  workflowStatus.textContent = "Placeholders such as {skill} become typed parameters; skills resolve from BEEHAIIVE_SKILLS_DIRS and conditions are validated before Save.";
});
document.querySelector("#workflow-advanced").addEventListener("change", (event) => {
  workflowJson.hidden = !event.target.checked;
});
workflowJson.addEventListener("change", () => {
  try {
    currentDefinition = JSON.parse(workflowJson.value);
    renderWorkflowEditor();
  } catch (error) {
    workflowErrors.textContent = error.message;
  }
});

for (const tab of document.querySelectorAll("[data-tab]")) {
  tab.addEventListener("click", () => {
    for (const candidate of document.querySelectorAll("[data-tab]")) {
      candidate.classList.toggle("active", candidate === tab);
    }
    for (const panel of document.querySelectorAll("[data-panel]")) {
      panel.classList.toggle("active", panel.dataset.panel === tab.dataset.tab);
    }
    if (tab.dataset.tab === "workflows") {
      loadWorkflows().catch((error) => { workflowStatus.textContent = error.message; });
    }
  });
}

load().catch((error) => {
  statusText.textContent = error.message;
});
