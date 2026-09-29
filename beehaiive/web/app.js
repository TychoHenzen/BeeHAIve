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
const agentList = document.querySelector("#agent-list");
const agentsStatus = document.querySelector("#agents-status");
const agentForm = document.querySelector("#agent-form");
const agentFormTitle = document.querySelector("#agent-form-title");
const agentName = document.querySelector("#agent-name");
const agentWorkflow = document.querySelector("#agent-workflow");
const agentParameters = document.querySelector("#agent-parameters");
const agentRepository = document.querySelector("#agent-repository");
const agentCheckout = document.querySelector("#agent-checkout");
const agentModel = document.querySelector("#agent-model");
let currentWorkflowId = null;
let currentDefinition = null;
let validationRevision = 0;
let agentWorkflows = [];
let editingAgentId = null;
let agentFormValues = {};
const logPollers = new Set();

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
      holder.textContent = item.holder?.name ? `held by ${item.holder.name}` : "unclaimed";
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
    source_prompt: "Wait for a backlog item, then run the selected skill.",
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

function exampleWorkflow() {
  const skills = [
    ["refine", "refine-backlog-item"],
    ["next", "next-ticket"],
    ["submit", "submit-draft-pr"],
    ["review", "review-pr"],
    ["fix", "fix-pr-review"],
    ["complete", "complete-pr"],
  ];
  const parameters = [
    { name: "backlog", type: "status", const: true, value: "Backlog" },
    ...skills.map(([name, value]) => ({ name: `${name}_skill`, type: "skill", const: true, value })),
  ];
  const runState = (id, title, skill, prompt, outcomes, x, y) => ({
    id,
    title,
    action: "run_skill",
    skill: `{${skill}_skill}`,
    prompt,
    outcomes,
    max_visits: 3,
    layout: { x, y },
  });
  return {
    schema_version: 1,
    name: "Operator delivery lifecycle",
    description: "Refine, implement, review, repair, and complete one held Project item.",
    source_prompt: "Refine a Backlog item, start its ticket, submit a draft PR, review it, repair findings, complete it, and escalate blockers.",
    auto_reset_on_stall: true,
    max_steps_per_pass: 20,
    parameters,
    initial: "wait",
    states: [
      { id: "wait", title: "Wait for Backlog", action: "wait_for_work", max_visits: 3, layout: { x: 0, y: 170 } },
      runState("refine", "Refine backlog item", "refine", "Research the held PBI and move it to Todo.", ["done", "blocked"], 300, 0),
      runState("next", "Start next ticket", "next", "Implement the refined PBI and push its branch.", ["done", "blocked"], 600, 0),
      runState("submit", "Submit draft PR", "submit", "Create or update the draft pull request.", ["done", "blocked"], 900, 0),
      runState("review", "Review PR", "review", "Review the branch against the live acceptance contract.", ["approved", "changes_requested", "blocked"], 1200, 0),
      runState("fix", "Fix review findings", "fix", "Apply only valid findings, verify them, and reply.", ["fixed", "blocked"], 1200, 260),
      runState("complete", "Complete PR", "complete", "Run the guarded checks, merge, and finalize the linked issue.", ["done", "blocked"], 900, 260),
      { id: "escalate", title: "Escalate blocker", action: "escalate", max_visits: 3, layout: { x: 600, y: 260 } },
    ],
    transitions: [
      { from: "wait", to: "refine", priority: 1, conditions: [{ kind: "item_status_is", value: "{backlog}" }] },
      { from: "refine", to: "next", priority: 1, conditions: [{ kind: "outcome_is", value: "done" }] },
      { from: "refine", to: "escalate", priority: 2, conditions: [{ kind: "outcome_is", value: "blocked" }] },
      { from: "next", to: "submit", priority: 1, conditions: [{ kind: "outcome_is", value: "done" }] },
      { from: "next", to: "escalate", priority: 2, conditions: [{ kind: "outcome_is", value: "blocked" }] },
      { from: "submit", to: "review", priority: 1, conditions: [{ kind: "outcome_is", value: "done" }] },
      { from: "submit", to: "escalate", priority: 2, conditions: [{ kind: "outcome_is", value: "blocked" }] },
      { from: "review", to: "complete", priority: 1, conditions: [{ kind: "outcome_is", value: "approved" }] },
      { from: "review", to: "fix", priority: 2, conditions: [{ kind: "outcome_is", value: "changes_requested" }] },
      { from: "review", to: "escalate", priority: 3, conditions: [{ kind: "outcome_is", value: "blocked" }] },
      { from: "fix", to: "review", priority: 1, conditions: [{ kind: "outcome_is", value: "fixed" }] },
      { from: "fix", to: "escalate", priority: 2, conditions: [{ kind: "outcome_is", value: "blocked" }] },
      { from: "complete", to: "wait", priority: 1, conditions: [{ kind: "outcome_is", value: "done" }] },
      { from: "complete", to: "escalate", priority: 2, conditions: [{ kind: "outcome_is", value: "blocked" }] },
      { from: "escalate", to: "wait", priority: 1, conditions: [{ kind: "always" }] },
    ],
  };
}

function showEditor(definition, workflowId = null) {
  currentWorkflowId = workflowId;
  currentDefinition = structuredClone(definition);
  if (!("parameters" in currentDefinition)) currentDefinition.parameters = [];
  if (!("states" in currentDefinition)) currentDefinition.states = [];
  if (!("transitions" in currentDefinition)) currentDefinition.transitions = [];
  workflowName.value = text(currentDefinition.name);
  workflowPrompt.value = text(currentDefinition.source_prompt);
  workflowEditor.hidden = false;
  workflowList.hidden = true;
  renderWorkflowEditor();
}

const conditionKinds = [
  "item_status_is",
  "item_type_is",
  "item_has_label",
  "item_lacks_label",
  "item_repository_is",
  "outcome_is",
  "always",
];

function transitionConditionFor(state) {
  if (state?.action === "wait_for_work") {
    const parameters = Array.isArray(currentDefinition.parameters)
      ? currentDefinition.parameters
      : [];
    const parameter = parameters.find((candidate) => candidate.type === "status");
    return { kind: "item_status_is", value: parameter ? `{${parameter.name}}` : "Backlog" };
  }
  if (state?.action === "run_skill") {
    return { kind: "outcome_is", value: text(state.outcomes?.[0] || "done") };
  }
  return { kind: "always" };
}

function makeOption(documentObject, value, selectedValue) {
  const item = documentObject.createElement("option");
  item.value = value;
  item.textContent = value;
  item.selected = value === selectedValue;
  return item;
}

function renderWorkflowEditor() {
  if (!currentDefinition) return;
  workflowJson.value = JSON.stringify(currentDefinition, null, 2);
  workflowParameters.replaceChildren();
  const parameters = Array.isArray(currentDefinition.parameters)
    ? currentDefinition.parameters
    : [];
  for (const [index, parameter] of parameters.entries()) {
    const row = document.createElement("div");
    row.className = "parameter-row";
    row.dataset.parameterIndex = index;
    row.dataset.parameterName = text(parameter.name);
    const name = document.createElement("input");
    name.value = text(parameter.name);
    name.placeholder = "parameter name";
    name.setAttribute("aria-label", `${parameter.name || "parameter"} name`);
    name.addEventListener("change", () => {
      parameter.name = name.value;
      row.dataset.parameterName = name.value;
      validateCurrent();
    });
    const type = document.createElement("select");
    for (const option of ["status", "label", "skill", "repository", "item_type", "text", "number", "boolean"]) {
      type.append(makeOption(document, option, parameter.type));
    }
    type.setAttribute("aria-label", `${parameter.name || "parameter"} type`);
    type.addEventListener("change", () => { parameter.type = type.value; validateCurrent(); });
    const constant = document.createElement("input");
    constant.type = "checkbox";
    constant.checked = parameter.const === true;
    constant.setAttribute("aria-label", `${parameter.name || "parameter"} constant`);
    constant.addEventListener("change", () => {
      parameter.const = constant.checked;
      parameter.value = constant.checked ? (parameter.value ?? "") : null;
      renderWorkflowEditor();
    });
    const value = document.createElement("input");
    value.value = text(parameter.value);
    value.disabled = !constant.checked;
    value.placeholder = constant.checked ? "constant value" : "assigned at runtime";
    value.setAttribute("aria-label", `${parameter.name || "parameter"} value`);
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
  const states = Array.isArray(currentDefinition.states) ? currentDefinition.states : [];
  const stateById = new Map(states.map((state) => [state.id, state]));
  for (const [stateIndex, state] of states.entries()) {
    state.layout ??= { x: 0, y: stateIndex * 160 };
  }
  const canvasHeight = Math.max(420, ...states.map((state) => Number(state.layout?.y ?? 0) + 190)) + 230;
  const canvasWidth = Math.max(960, ...states.map((state) => Number(state.layout?.x ?? 0) + 280));
  const transitionsForCanvas = Array.isArray(currentDefinition.transitions)
    ? currentDefinition.transitions
    : [];
  workflowCanvas.style.minHeight = `${canvasHeight}px`;
  const edgeLayer = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  edgeLayer.classList.add("workflow-edges");
  edgeLayer.setAttribute("aria-hidden", "true");
  edgeLayer.setAttribute("viewBox", `0 0 ${canvasWidth} ${canvasHeight}`);
  edgeLayer.setAttribute("width", canvasWidth);
  edgeLayer.setAttribute("height", canvasHeight);
  for (const transition of transitionsForCanvas) {
    const source = stateById.get(transition.from);
    const target = stateById.get(transition.to);
    if (!source || !target) continue;
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", Number(source.layout?.x ?? 0) + 115);
    line.setAttribute("y1", Number(source.layout?.y ?? 0) + 70);
    line.setAttribute("x2", Number(target.layout?.x ?? 0) + 115);
    line.setAttribute("y2", Number(target.layout?.y ?? 0) + 70);
    edgeLayer.append(line);
  }
  workflowCanvas.append(edgeLayer);
  for (const [stateIndex, state] of states.entries()) {
    const card = document.createElement("article");
    card.className = "state-card";
    card.dataset.stateId = text(state.id);
    card.dataset.stateIndex = stateIndex;
    card.style.left = `${Number(state.layout.x ?? 0)}px`;
    card.style.top = `${Number(state.layout.y ?? 0)}px`;
    const title = document.createElement("h3");
    title.textContent = `${state.id === currentDefinition.initial ? "★ " : ""}${text(state.title || state.id)}`;
    const titleInput = document.createElement("input");
    titleInput.value = text(state.title || state.id);
    titleInput.setAttribute("aria-label", `${state.id} title`);
    titleInput.addEventListener("change", () => { state.title = titleInput.value; renderWorkflowEditor(); });
    const actionSelect = document.createElement("select");
    for (const option of ["wait_for_work", "run_skill", "escalate"]) {
      actionSelect.append(makeOption(document, option, state.action));
    }
    actionSelect.setAttribute("aria-label", `${state.id} action`);
    actionSelect.addEventListener("change", () => { state.action = actionSelect.value; validateCurrent(); });
    const fields = [titleInput, actionSelect];
    if (state.action === "run_skill") {
      const skillInput = document.createElement("input");
      skillInput.value = text(state.skill);
      skillInput.placeholder = "skill or {skill_parameter}";
      skillInput.setAttribute("aria-label", `${state.id} skill`);
      skillInput.addEventListener("change", () => { state.skill = skillInput.value; validateCurrent(); });
      const promptInput = document.createElement("input");
      promptInput.value = text(state.prompt);
      promptInput.placeholder = "state-specific prompt";
      promptInput.setAttribute("aria-label", `${state.id} prompt`);
      promptInput.addEventListener("change", () => { state.prompt = promptInput.value; validateCurrent(); });
      const outcomesInput = document.createElement("input");
      outcomesInput.value = (state.outcomes ?? []).join(", ");
      outcomesInput.placeholder = "outcomes, comma separated";
      outcomesInput.setAttribute("aria-label", `${state.id} outcomes`);
      outcomesInput.addEventListener("change", () => {
        state.outcomes = outcomesInput.value.split(",").map((item) => item.trim()).filter(Boolean);
        renderWorkflowEditor();
      });
      fields.push(skillInput, promptInput, outcomesInput);
    }
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
    card.append(title, ...fields, edit, duplicate, remove);
    let drag = null;
    card.addEventListener("pointerdown", (event) => {
      const tag = String(event.target?.tagName ?? "").toLowerCase();
      if (["input", "select", "textarea", "button"].includes(tag)) return;
      const bounds = workflowCanvas.getBoundingClientRect();
      drag = {
        pointerId: event.pointerId,
        offsetX: event.clientX - bounds.left - Number(state.layout.x),
        offsetY: event.clientY - bounds.top - Number(state.layout.y),
      };
      card.setPointerCapture?.(event.pointerId);
    });
    card.addEventListener("pointermove", (event) => {
      if (!drag || drag.pointerId !== event.pointerId) return;
      const bounds = workflowCanvas.getBoundingClientRect();
      state.layout = {
        x: Math.max(0, Math.round(event.clientX - bounds.left - drag.offsetX)),
        y: Math.max(0, Math.round(event.clientY - bounds.top - drag.offsetY)),
      };
      card.style.left = `${state.layout.x}px`;
      card.style.top = `${state.layout.y}px`;
      workflowJson.value = JSON.stringify(currentDefinition, null, 2);
    });
    const stopDrag = () => {
      if (drag) renderWorkflowEditor();
      drag = null;
    };
    card.addEventListener("pointerup", stopDrag);
    card.addEventListener("pointercancel", stopDrag);
    workflowCanvas.append(card);
  }
  const transitions = document.createElement("div");
  transitions.className = "workflow-transitions";
  transitions.style.top = `${canvasHeight - 210}px`;
  transitions.dataset.editorSection = "transitions";
  for (const [index, transition] of transitionsForCanvas.entries()) {
    const edge = document.createElement("div");
    edge.className = "transition-card";
    edge.dataset.transitionIndex = index;
    const route = document.createElement("div");
    route.className = "transition-route";
    const from = document.createElement("select");
    from.setAttribute("aria-label", `transition ${index} source`);
    for (const state of states) from.append(makeOption(document, state.id, transition.from));
    from.addEventListener("change", () => { transition.from = from.value; renderWorkflowEditor(); });
    const to = document.createElement("select");
    to.setAttribute("aria-label", `transition ${index} target`);
    for (const state of states) to.append(makeOption(document, state.id, transition.to));
    to.addEventListener("change", () => { transition.to = to.value; renderWorkflowEditor(); });
    const priority = document.createElement("input");
    priority.type = "number";
    priority.min = "0";
    priority.value = text(transition.priority);
    priority.setAttribute("aria-label", `transition ${index} priority`);
    priority.addEventListener("change", () => { transition.priority = Number(priority.value); validateCurrent(); });
    route.append(from, document.createTextNode(" → "), to, priority);
    const conditions = document.createElement("div");
    conditions.className = "transition-conditions";
    for (const [conditionIndex, condition] of (transition.conditions ?? []).entries()) {
      const row = document.createElement("div");
      row.className = "condition-row";
      row.dataset.conditionIndex = conditionIndex;
      const kind = document.createElement("select");
      kind.setAttribute("aria-label", `transition ${index} condition ${conditionIndex} kind`);
      for (const value of conditionKinds) kind.append(makeOption(document, value, condition.kind));
      kind.addEventListener("change", () => {
        condition.kind = kind.value;
        if (kind.value === "always") delete condition.value;
        else condition.value = condition.value ?? "";
        renderWorkflowEditor();
      });
      const value = document.createElement("input");
      value.value = text(condition.value);
      value.disabled = condition.kind === "always";
      value.placeholder = condition.kind === "always" ? "no value" : "literal or {parameter}";
      value.setAttribute("aria-label", `transition ${index} condition ${conditionIndex} value`);
      value.addEventListener("change", () => { condition.value = value.value; validateCurrent(); });
      const removeCondition = document.createElement("button");
      removeCondition.type = "button";
      removeCondition.textContent = "Remove condition";
      removeCondition.addEventListener("click", () => {
        transition.conditions.splice(conditionIndex, 1);
        renderWorkflowEditor();
      });
      row.append(kind, value, removeCondition);
      conditions.append(row);
    }
    const addCondition = document.createElement("button");
    addCondition.type = "button";
    addCondition.textContent = "Add condition";
    addCondition.addEventListener("click", () => {
      const state = stateById.get(transition.from);
      transition.conditions ??= [];
      transition.conditions.push(transitionConditionFor(state));
      renderWorkflowEditor();
    });
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "Delete";
    remove.addEventListener("click", () => {
      currentDefinition.transitions.splice(index, 1);
      renderWorkflowEditor();
    });
    edge.append(route, conditions, addCondition, remove);
    transitions.append(edge);
  }
  workflowCanvas.append(transitions);
  workflowErrors.textContent = "";
  validateCurrent();
}

function markValidationErrors(issues) {
  for (const element of document.querySelectorAll("[aria-invalid=\"true\"]")) {
    element.removeAttribute("aria-invalid");
    delete element.dataset.validationError;
  }
  const findByData = (attribute, value) => {
    const datasetKey = attribute
      .replace("data-", "")
      .replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
    return [...document.querySelectorAll(`[${attribute}]`)]
      .find((element) => String(element.dataset[datasetKey]) === String(value));
  };
  for (const issue of issues) {
    const stateMatch = issue.location.match(/^states\[([^\]]+)\]/);
    const parameterMatch = issue.location.match(/^parameters\[([^\]]+)\]/);
    const transitionMatch = issue.location.match(/^transitions\[(\d+)\]/);
    let element = null;
    if (stateMatch) {
      element = findByData("data-state-index", stateMatch[1])
        ?? findByData("data-state-id", stateMatch[1]);
    }
    if (parameterMatch) {
      element = findByData("data-parameter-index", parameterMatch[1])
        ?? findByData("data-parameter-name", parameterMatch[1]);
    }
    if (transitionMatch) element = document.querySelector(`[data-transition-index=\"${transitionMatch[1]}\"]`);
    if (element) {
      element.setAttribute("aria-invalid", "true");
      element.dataset.validationError = issue.message;
    }
  }
}

async function validateCurrent() {
  if (!currentDefinition) return;
  const revision = ++validationRevision;
  try {
    const result = await workflowRequest("/api/workflows/validate", {
      method: "POST",
      body: JSON.stringify({ definition: currentDefinition }),
    });
    if (revision !== validationRevision) return;
    workflowSave.disabled = !result.valid;
    const issues = [...result.errors, ...result.warnings];
    markValidationErrors(issues);
    workflowErrors.textContent = issues.length
      ? issues.map((issue) => `${issue.level ?? "error"} ${issue.location}: ${issue.message}`).join("\n")
      : "Definition is valid.";
  } catch (error) {
    if (revision !== validationRevision) return;
    workflowSave.disabled = true;
    markValidationErrors([]);
    workflowErrors.textContent = error.message;
  }
}

function duplicateState(stateId) {
  if (!Array.isArray(currentDefinition.states)) currentDefinition.states = [];
  const state = currentDefinition.states.find((candidate) => candidate.id === stateId);
  if (!state) return;
  const copy = structuredClone(state);
  const existingIds = new Set(currentDefinition.states.map((candidate) => candidate.id));
  let suffix = 1;
  copy.id = `${stateId}_copy`;
  while (existingIds.has(copy.id)) copy.id = `${stateId}_copy_${++suffix}`;
  copy.title = `${text(copy.title)} copy`;
  currentDefinition.states.push(copy);
  renderWorkflowEditor();
}

function deleteState(stateId) {
  if (stateId === currentDefinition.initial) return;
  if (!Array.isArray(currentDefinition.states)) currentDefinition.states = [];
  if (!Array.isArray(currentDefinition.transitions)) currentDefinition.transitions = [];
  currentDefinition.states = currentDefinition.states.filter((state) => state.id !== stateId);
  currentDefinition.transitions = currentDefinition.transitions.filter((edge) => edge.from !== stateId && edge.to !== stateId);
  renderWorkflowEditor();
}

function addState() {
  if (!Array.isArray(currentDefinition.states)) currentDefinition.states = [];
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
  if (!Array.isArray(currentDefinition.states)) currentDefinition.states = [];
  if (!Array.isArray(currentDefinition.transitions)) currentDefinition.transitions = [];
  const states = currentDefinition.states;
  if (states.length < 2) return;
  const source = states.find((state) => state.id === currentDefinition.initial) ?? states[0];
  const target = states.find((state) => state.id !== source.id) ?? states[0];
  currentDefinition.transitions.push({
    from: source.id,
    to: target.id,
    priority: currentDefinition.transitions.length + 1,
    conditions: [transitionConditionFor(source)],
  });
  renderWorkflowEditor();
}

function addParameter() {
  if (!Array.isArray(currentDefinition.parameters)) currentDefinition.parameters = [];
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

async function loadAgentWorkflows() {
  if (!agentWorkflow) return;
  const payload = await workflowRequest("/api/workflows");
  agentWorkflows = await Promise.all(
    (payload.workflows ?? []).map((workflow) => workflowRequest(`/api/workflows/${workflow.id}`)),
  );
  agentWorkflow.replaceChildren();
  for (const workflow of agentWorkflows) {
    const option = makeOption(document, `${workflow.id}`, "");
    option.textContent = `${workflow.name} · #${workflow.id}`;
    agentWorkflow.append(option);
  }
  if (editingAgentId === null && !agentWorkflow.value && agentWorkflows.length) {
    agentWorkflow.value = text(agentWorkflows[0].id);
  }
  renderAgentParameters();
}

function selectedAgentWorkflow() {
  return agentWorkflows.find((workflow) => String(workflow.id) === String(agentWorkflow?.value)) ?? null;
}

function renderAgentParameters() {
  if (!agentParameters) return;
  agentParameters.replaceChildren();
  const definition = selectedAgentWorkflow()?.latest?.definition;
  for (const parameter of definition?.parameters ?? []) {
    const row = document.createElement("label");
    row.className = "agent-parameter";
    row.textContent = `${text(parameter.name)} (${text(parameter.type)})`;
    if (parameter.const === true) {
      const constant = document.createElement("span");
      constant.textContent = `constant: ${text(parameter.value)}`;
      row.append(constant);
    } else {
      const input = document.createElement("input");
      input.dataset.agentParameterName = text(parameter.name);
      input.type = parameter.type === "boolean" ? "checkbox" : parameter.type === "number" ? "number" : "text";
      if (input.type === "checkbox") input.checked = agentFormValues[parameter.name] === true;
      else input.value = text(agentFormValues[parameter.name]);
      input.required = parameter.type !== "boolean";
      row.append(input);
    }
    agentParameters.append(row);
  }
}

function readAgentParameters() {
  const values = {};
  for (const input of agentParameters?.querySelectorAll("[data-agent-parameter-name]") ?? []) {
    const name = input.dataset.agentParameterName;
    if (!name) continue;
    if (input.type === "checkbox") values[name] = input.checked;
    else if (input.type === "number") values[name] = Number(input.value);
    else values[name] = input.value;
  }
  return values;
}

function openAgentForm(agent = null) {
  if (!agentForm) return;
  editingAgentId = agent?.id ?? null;
  agentFormTitle.textContent = editingAgentId ? "Edit agent" : "Create agent";
  agentName.value = text(agent?.name);
  agentWorkflow.value = text(agent?.workflow_id || agentWorkflows[0]?.id);
  agentRepository.value = text(agent?.repository);
  agentCheckout.value = text(agent?.checkout_path);
  agentModel.value = text(agent?.model);
  agentFormValues = { ...(agent?.parameters ?? {}) };
  agentForm.hidden = false;
  renderAgentParameters();
}

function closeAgentForm() {
  if (!agentForm) return;
  editingAgentId = null;
  agentForm.hidden = true;
}

async function saveAgent(event) {
  event.preventDefault();
  const body = {
    name: agentName.value,
    workflow_id: Number(agentWorkflow.value),
    parameters: readAgentParameters(),
    repository: agentRepository.value,
    checkout_path: agentCheckout.value,
    model: agentModel.value || null,
  };
  const path = editingAgentId ? `/api/agents/${editingAgentId}` : "/api/agents";
  await workflowRequest(path, {
    method: editingAgentId ? "PATCH" : "POST",
    body: JSON.stringify(body),
  });
  closeAgentForm();
  await loadAgents();
}

function renderAgents(payload) {
  for (const stop of logPollers) stop();
  logPollers.clear();
  agentList.replaceChildren();
  const agents = payload.agents ?? [];
  if (!agents.length) {
    agentList.textContent = "No assigned agents.";
    return;
  }
  for (const agent of agents) {
    const card = document.createElement("article");
    card.className = `agent-card agent-${text(agent.status)}`;
    const title = document.createElement("h3");
    title.textContent = `${text(agent.name)} · ${text(agent.status)}`;
    const detail = document.createElement("p");
    const held = agent.current_pass?.item?.title ? ` · ${agent.current_pass.item.title}` : "";
    const state = agent.current_state ? ` · state ${text(agent.current_state)}` : "";
    detail.textContent = `${text(agent.repository)} · workflow ${text(agent.workflow_id)} r${text(agent.workflow_revision)}${state}${held}`;
    const actions = document.createElement("div");
    actions.className = "agent-actions";
    const start = document.createElement("button");
    start.type = "button";
    start.textContent = "Start";
    start.disabled = ["working", "stalled"].includes(agent.status);
    start.addEventListener("click", () => agentRequest(`/api/agents/${agent.id}/start`));
    const stop = document.createElement("button");
    stop.type = "button";
    stop.textContent = "Stop";
    stop.disabled = agent.status === "stopped";
    stop.addEventListener("click", () => agentRequest(`/api/agents/${agent.id}/stop`));
    const reset = document.createElement("button");
    reset.type = "button";
    reset.textContent = "Reset stall";
    reset.disabled = agent.status !== "stalled";
    reset.addEventListener("click", () => agentRequest(`/api/agents/${agent.id}/reset`));
    const edit = document.createElement("button");
    edit.type = "button";
    edit.textContent = "Edit";
    edit.disabled = agent.status !== "stopped";
    edit.addEventListener("click", async () => {
      try {
        await loadAgentWorkflows();
        openAgentForm(agent);
      } catch (error) {
        agentsStatus.textContent = error.message;
      }
    });
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "Delete";
    remove.disabled = agent.status !== "stopped";
    remove.addEventListener("click", () => agentRequest(`/api/agents/${agent.id}`, "DELETE"));
    const logs = document.createElement("button");
    logs.type = "button";
    logs.textContent = "Log tail";
    logs.disabled = !agent.history?.length;
    logs.addEventListener("click", async () => {
      const response = await fetch(`/api/agents/${agent.id}/logs`);
      const payload = await response.json();
      if (!response.ok) {
        agentsStatus.textContent = payload.detail || `Log request failed: ${response.status}`;
        return;
      }
      const output = document.createElement("pre");
      output.textContent = payload.lines?.join("\n") || "No log events recorded.";
      card.append(output);
    });
    actions.append(start, stop, reset, edit, remove, logs);
    card.append(title, detail, actions);
    const history = document.createElement("details");
    const historySummary = document.createElement("summary");
    historySummary.textContent = `${agent.history?.length ?? 0} passes · ${agent.alerts?.length ?? 0} alerts`;
    history.append(historySummary);
    for (const pass of agent.history ?? []) {
      const passDetails = document.createElement("div");
      passDetails.textContent = `Pass ${text(pass.id)} · ${text(pass.status)} · ${text(pass.item?.title)}`;
      for (const step of pass.steps ?? []) {
        const stepDetails = document.createElement("p");
        stepDetails.textContent = `Step ${text(step.sequence)} ${text(step.state_id)} · ${text(step.status)} · ${text(step.outcome)} · ${text(step.summary)}`;
        passDetails.append(stepDetails);
      }
      history.append(passDetails);
    }
    if ((agent.alerts ?? []).length) {
      const alerts = document.createElement("ul");
      for (const alert of agent.alerts) {
        const item = document.createElement("li");
        item.textContent = `${text(alert.kind)}: ${text(alert.message)}`;
        alerts.append(item);
      }
      history.append(alerts);
    }
    card.append(history);
    if (agent.last_error) {
      const error = document.createElement("pre");
      error.textContent = text(agent.last_error);
      card.append(error);
    }
    if (agent.status === "working" && agent.current_pass_id) {
      const output = document.createElement("pre");
      output.className = "agent-log-tail";
      output.textContent = "Loading live log tail…";
      card.append(output);
      const refreshLog = async () => {
        try {
          const response = await fetch(`/api/agents/${agent.id}/logs?pass_id=${encodeURIComponent(agent.current_pass_id)}`);
          const result = await response.json();
          if (response.ok) output.textContent = result.lines?.join("\n") || "No log events recorded.";
        } catch (error) {
          output.textContent = error.message;
        }
      };
      refreshLog();
      const timer = setInterval(refreshLog, 2000);
      logPollers.add(() => clearInterval(timer));
    }
    agentList.append(card);
  }
}

async function loadAgents() {
  agentsStatus.textContent = "Loading agent state…";
  try {
    const response = await fetch("/api/agents");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || `Agent request failed: ${response.status}`);
    renderAgents(payload);
    agentsStatus.textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    agentsStatus.textContent = error.message;
  }
}

async function agentRequest(path, method = "POST") {
  const response = await fetch(path, { method });
  const payload = await response.json();
  if (!response.ok) {
    agentsStatus.textContent = payload.detail || `Agent request failed: ${response.status}`;
    return;
  }
  await loadAgents();
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
document.querySelector("#workflow-example").addEventListener("click", () => showEditor(exampleWorkflow()));
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
    if (tab.dataset.tab === "agents") {
      loadAgentWorkflows().catch((error) => { agentsStatus.textContent = error.message; });
      loadAgents();
    }
  });
}

const agentsRefresh = document.querySelector("#agents-refresh");
if (agentsRefresh) agentsRefresh.addEventListener("click", () => loadAgents());
const agentNew = document.querySelector("#agent-new");
if (agentNew) {
  agentNew.addEventListener("click", async () => {
    try {
      await loadAgentWorkflows();
      openAgentForm();
    } catch (error) {
      agentsStatus.textContent = error.message;
    }
  });
}
if (agentWorkflow) agentWorkflow.addEventListener("change", renderAgentParameters);
if (agentForm) agentForm.addEventListener("submit", (event) => saveAgent(event).catch((error) => { agentsStatus.textContent = error.message; }));
const agentCancel = document.querySelector("#agent-cancel");
if (agentCancel) agentCancel.addEventListener("click", closeAgentForm);

load().catch((error) => {
  statusText.textContent = error.message;
});
