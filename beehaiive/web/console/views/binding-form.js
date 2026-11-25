// The fields of a trigger binding, as choices wherever Archon or the other
// bindings already know the answer. The project sets the folder, the folder
// lists its workflows, the workflow declares its inputs, and the resource is
// a new one or one another binding already claims. Free text is left only for
// a folder no project has and a resource no binding has.
import { el, notice, replace } from "../dom.js";
import {
  bindingInputOptions,
  bindingInputs,
  capacityConflicts,
  inputRows,
  intervalParts,
  intervalSeconds,
  projectForFolder,
  resourceChoices,
  SELF_BINDING,
  suggestedResource,
  workflowInputs,
  workflowSummary,
} from "../model.js";

const OTHER_FOLDER = "@folder";
const OTHER_RESOURCE = "@resource";

// A hint that is already an element keeps its own class, so it can turn into a warning.
export const label = (text, control, hint) =>
  el("label", { class: "field" }, el("span", {}, text), control, hint instanceof Node ? hint : hint ? el("span", { class: "muted small" }, hint) : null);

const options = (choices, selected) => choices.map(({ value, label: text }) => el("option", { value, selected: value === selected }, text));

// Each folder's workflows, fetched once per page.
export function workflowCache(api) {
  const kept = new Map();
  return (cwd) => {
    if (!kept.has(cwd)) {
      const loading = api.workflows(cwd).then((listing) => (listing.workflows ?? []).map((entry) => entry.workflow).sort((a, b) => a.name.localeCompare(b.name)));
      loading.catch(() => kept.delete(cwd));
      kept.set(cwd, loading);
    }
    return kept.get(cwd);
  };
}

// page: { projects, bindings, workflowsFor }. self is the binding's name (null
// while it has none), selfPath() its file ("" while it has no name).
// onProject(project) runs when the person picks a project; when it returns
// true it has refilled the fields itself.
export function bindingFields(page, { self, selfPath, onProject = null }) {
  const { projects, bindings } = page;
  const state = { cwd: "", workflow: "", workflows: null, loadError: null, rows: [], resourcePicked: false };
  const hosts = [...new Set(["local", ...bindings.map((b) => b.hostId).filter(Boolean)])];
  const hostList = `hosts-${Math.random().toString(36).slice(2)}`;

  const fields = {
    project: el("select", {}),
    folder: el("input", { type: "text", placeholder: "C:\\path\\to\\checkout" }),
    workflow: el("select", { required: true }),
    amount: el("input", { type: "number", min: "1", step: "any", value: "30" }),
    unit: el("select", {}, options([{ value: "minutes", label: "minutes" }, { value: "hours", label: "hours" }], "minutes")),
    resource: el("select", {}),
    customResource: el("input", { type: "text", placeholder: "kind:owner/repo:queue" }),
    capacity: el("input", { type: "number", min: "1", max: "32", value: "1" }),
    overlap: el("select", {}, options([{ value: "skip", label: "skip" }, { value: "queue", label: "queue" }], "skip")),
    isolation: el("select", {}, options(["worktree", "default", "in-place"].map((kind) => ({ value: kind, label: kind })), "worktree")),
    hostId: el("input", { type: "text", value: "local", list: hostList }),
  };
  const folderField = label("Folder", fields.folder, "A checkout that is not one of Archon's projects.");
  const workflowNote = el("span", { class: "muted small" });
  const customResourceField = label("New resource", fields.customResource);
  const resourceNote = el("span", { class: "muted small" });
  const capacityNote = el("span", { class: "small" });
  const inputsSlot = el("fieldset", {});

  const project = () => projects.find((p) => p.id === fields.project.value) ?? null;
  const declared = () => {
    const chosen = state.workflows?.find((w) => w.name === state.workflow);
    return chosen ? workflowInputs(chosen) : null;
  };
  const resource = () => (fields.resource.value === OTHER_RESOURCE ? fields.customResource.value.trim() : fields.resource.value);
  const suggestion = () => suggestedResource(state.workflow, project(), fields.hostId.value.trim() || "local");

  function renderProjects(selected) {
    replace(
      fields.project,
      projects.map((p) => el("option", { value: p.id, selected: p.id === selected }, p.name)),
      el("option", { value: OTHER_FOLDER, selected: selected === OTHER_FOLDER }, "Another folder..."),
    );
    folderField.hidden = selected !== OTHER_FOLDER;
  }

  function renderWorkflows() {
    const known = state.workflows ?? [];
    const choices = known.map((w) => ({ value: w.name, label: w.name }));
    if (state.workflow && !known.some((w) => w.name === state.workflow)) {
      choices.unshift({ value: state.workflow, label: state.workflows ? `${state.workflow} (not found in this folder)` : state.workflow });
    }
    if (!state.workflow) choices.unshift({ value: "", label: state.workflows ? "Pick a workflow" : "Loading workflows..." });
    replace(fields.workflow, options(choices, state.workflow));
    const chosen = known.find((w) => w.name === state.workflow);
    replace(
      workflowNote,
      state.loadError ? `Archon did not list this folder's workflows: ${state.loadError}` : chosen ? workflowSummary(chosen.description) : "",
    );
  }

  function renderResource(current) {
    const choices = resourceChoices(bindings, { self, suggested: suggestion(), current });
    const describe = (choice) =>
      choice.sharedWith.length ? `${choice.value}  (shared with ${choice.sharedWith.join(", ")})` : choice.value;
    replace(
      fields.resource,
      options(choices.map((choice) => ({ value: choice.value, label: describe(choice) })), current),
      el("option", { value: OTHER_RESOURCE }, "A new resource..."),
    );
    if (!choices.length) fields.resource.value = OTHER_RESOURCE;
    renderResourceNotes();
  }

  function renderResourceNotes() {
    const custom = fields.resource.value === OTHER_RESOURCE;
    customResourceField.hidden = !custom;
    const chosen = resourceChoices(bindings, { self, suggested: "", current: "" }).find((c) => c.value === resource());
    replace(
      resourceNote,
      chosen
        ? `Shared with ${chosen.sharedWith.join(", ")}: their runs count against one capacity.`
        : "Only this binding claims it, so its capacity limits its own runs.",
    );
    const conflicts = capacityConflicts(bindings, { resource: resource(), capacity: Number(fields.capacity.value) });
    const who = (c) => (c.name === self ? `this binding was saved with ${c.capacity}` : `${c.name} has ${c.capacity}`);
    capacityNote.className = conflicts.length ? "small warn" : "small muted";
    replace(
      capacityNote,
      conflicts.length
        ? `On this resource ${conflicts.map(who).join(", ")}. Archon fixes a resource's capacity the first time a binding fires on it and rejects every fire that declares another. Keep ${conflicts[0].capacity}, or pick a new resource for a new capacity.`
        : "How many runs on this resource may go at once. Archon fixes it the first time a binding fires on the resource.",
    );
  }

  function renderInputs() {
    const known = declared();
    replace(
      inputsSlot,
      el("legend", {}, "Inputs"),
      known === null && state.workflow && state.workflows !== null
        ? notice("This workflow's inputs are not known here, so the file's inputs show as they are.")
        : null,
      state.rows.length ? null : notice(state.workflow ? "This workflow takes no inputs." : "Pick a workflow to see its inputs."),
      state.rows.map(inputRow),
    );
  }

  function inputRow(row) {
    const name = el("span", {}, el("code", {}, row.name), row.required ? el("span", { class: "required" }, " required") : null);
    let control;
    if (row.kind === "binding") {
      const file = selfPath().split(/[\\/]/).pop();
      const choices = bindingInputOptions(bindings, { self, required: row.required, value: row.value }).map((choice) =>
        choice.value === SELF_BINDING && file ? { ...choice, label: `This binding (${file})` } : choice,
      );
      control = el("select", { onchange: (event) => (row.value = event.target.value) }, options(choices, row.value));
    } else {
      const placeholder = row.default !== null && row.default !== undefined ? `default: ${row.default || "(empty)"}` : row.required ? "required" : "";
      control = el("input", { type: "text", value: row.value, placeholder, oninput: (event) => (row.value = event.target.value) });
    }
    const remove = row.extra
      ? el("button", { type: "button", class: "linklike", onclick: () => ((state.rows = state.rows.filter((r) => r !== row)), renderInputs()) }, "Remove")
      : null;
    const hint = row.extra ? `Not an input of ${state.workflow}. Kept as the file has it.` : row.description || (row.kind === "binding" ? "Another binding's file." : "");
    return el("label", { class: "field" }, name, control, el("span", { class: "muted small" }, hint, remove ? " " : null, remove));
  }

  // The rows for the current workflow, keeping whatever was already filled in.
  function rebuildRows(values) {
    state.rows = inputRows(declared(), values, selfPath());
    renderInputs();
  }

  const currentValues = () => bindingInputs(state.rows, selfPath());

  async function loadWorkflows(values) {
    const cwd = state.cwd;
    state.workflows = null;
    state.loadError = null;
    renderWorkflows();
    rebuildRows(values);
    if (!cwd) return;
    try {
      const listed = await page.workflowsFor(cwd);
      if (state.cwd !== cwd) return;
      state.workflows = listed;
    } catch (error) {
      if (state.cwd !== cwd) return;
      state.workflows = [];
      state.loadError = error.message;
    }
    renderWorkflows();
    rebuildRows(values);
    if (!state.resourcePicked) renderResource(suggestion());
  }

  fields.project.addEventListener("change", () => {
    const chosen = project();
    folderField.hidden = fields.project.value !== OTHER_FOLDER;
    if (onProject && chosen && onProject(chosen)) return;
    state.cwd = chosen?.default_cwd ?? fields.folder.value.trim();
    loadWorkflows(currentValues());
  });
  fields.folder.addEventListener("change", () => {
    state.cwd = fields.folder.value.trim();
    loadWorkflows(currentValues());
  });
  fields.workflow.addEventListener("change", () => {
    const values = currentValues();
    state.workflow = fields.workflow.value;
    renderWorkflows();
    rebuildRows(values);
    if (!state.resourcePicked) renderResource(suggestion());
  });
  fields.resource.addEventListener("change", () => {
    state.resourcePicked = true;
    renderResourceNotes();
  });
  fields.customResource.addEventListener("input", renderResourceNotes);
  fields.capacity.addEventListener("input", renderResourceNotes);
  fields.hostId.addEventListener("change", () => {
    if (!state.resourcePicked) renderResource(suggestion());
  });

  renderProjects(projects[0]?.id ?? OTHER_FOLDER);

  return {
    fields,
    project,
    // Set every field from a binding or a preset. A saved binding's resource
    // counts as picked, so a change of workflow does not replace it.
    fill(values, { picked = true } = {}) {
      const owner = projectForFolder(projects, values.cwd);
      renderProjects(owner ? owner.id : values.cwd ? OTHER_FOLDER : (projects[0]?.id ?? OTHER_FOLDER));
      fields.folder.value = owner ? "" : (values.cwd ?? "");
      state.cwd = values.cwd || project()?.default_cwd || "";
      state.workflow = values.workflow ?? "";
      state.resourcePicked = picked;
      const { amount, unit } = intervalParts(values.intervalSeconds);
      fields.amount.value = String(amount);
      fields.unit.value = unit;
      fields.capacity.value = String(values.capacity ?? 1);
      fields.overlap.value = values.overlap ?? "skip";
      fields.isolation.value = values.isolation ?? "worktree";
      fields.hostId.value = values.hostId ?? "local";
      renderResource(values.resource || suggestion());
      return loadWorkflows(values.inputs ?? {});
    },
    // The request body without the name and run-as user.
    read() {
      if (!state.cwd) throw new Error("Pick a project or a folder.");
      if (!state.workflow) throw new Error("Pick a workflow.");
      const missing = state.rows.filter((row) => row.required && !String(row.value ?? "").trim()).map((row) => row.name);
      if (missing.length) throw new Error(`Fill in: ${missing.join(", ")}`);
      if (!resource()) throw new Error("Name the new resource.");
      return {
        workflow: state.workflow,
        cwd: state.cwd,
        resource: resource(),
        hostId: fields.hostId.value.trim(),
        intervalSeconds: intervalSeconds({ amount: fields.amount.value, unit: fields.unit.value }),
        overlap: fields.overlap.value,
        capacity: Number(fields.capacity.value),
        isolation: fields.isolation.value,
        inputs: currentValues(),
      };
    },
    // The binding's own file moved (a new name), so rows that point at it say so.
    renamed() {
      renderInputs();
    },
    labels: () => [
      label("Project", fields.project, "The binding's runs start in this project's folder."),
      folderField,
      label("Workflow", fields.workflow, workflowNote),
      label("Every", el("span", { class: "inline-fields" }, fields.amount, fields.unit)),
      label("Resource", fields.resource, resourceNote),
      customResourceField,
      label("Capacity", fields.capacity, capacityNote),
      label("Overlap", fields.overlap, "skip drops a start while the resource is full; queue waits for a slot."),
      label("Isolation", fields.isolation, "worktree gives each run its own checkout; default uses the project's setting."),
      label("Host id", el("span", {}, fields.hostId, el("datalist", { id: hostList }, hosts.map((h) => el("option", { value: h })))), "Names this machine to archon trigger drain."),
      inputsSlot,
    ],
  };
}
