// Start a workflow run in a project, with its declared inputs.
import { actionButton, el, notice, replace } from "../dom.js";
import { collectSuppliedInputs, missingRequiredInputs, workflowInputs } from "../model.js";

export default {
  id: "launch",
  title: "Launch",
  nav: true,
  match: (path) => (path === "/launch" ? {} : null),
  mount(root, params, ctx) {
    const state = { projects: [], project: null, workflows: [], workflow: null, values: {} };
    const projectSelect = el("select", { "aria-label": "Project" });
    const filter = el("input", { type: "search", placeholder: "Filter workflows", "aria-label": "Filter workflows" });
    const workflowList = el("div", { class: "workflow-list", role: "listbox", "aria-label": "Workflows" });
    const form = el("div", { class: "launch-form" });
    replace(
      root,
      el("div", { class: "page-head" }, el("h1", {}, "Launch a run")),
      el("div", { class: "launch" }, el("div", { class: "launch-pick" }, el("label", {}, "Project ", projectSelect), filter, workflowList), form),
    );

    function renderWorkflows() {
      const text = filter.value.trim().toLowerCase();
      const shown = state.workflows.filter((w) => !text || w.name.toLowerCase().includes(text) || (w.description ?? "").toLowerCase().includes(text));
      replace(
        workflowList,
        shown.length ? null : notice("No workflow matches."),
        shown.map((w) =>
          el(
            "button",
            {
              type: "button",
              role: "option",
              class: `workflow-item${state.workflow?.name === w.name ? " active" : ""}`,
              "aria-selected": state.workflow?.name === w.name ? "true" : "false",
              onclick: () => {
                state.workflow = w;
                state.values = {};
                renderWorkflows();
                renderForm();
              },
            },
            el("strong", {}, w.name),
            w.pack?.key ? el("span", { class: "pill" }, w.pack.key) : null,
            el("span", { class: "muted small" }, (w.description ?? "").split("\n")[0].slice(0, 140)),
          ),
        ),
      );
    }

    function renderForm() {
      const w = state.workflow;
      if (!w) {
        replace(form, notice("Pick a workflow."));
        return;
      }
      const declared = workflowInputs(w);
      const message = el("textarea", { rows: "4", placeholder: "What should this run do? (an issue, a request, a branch...)" });
      const fields = declared.map((input) => {
        const field = el("input", {
          type: "text",
          placeholder: input.default !== null ? `default: ${input.default || "(empty)"}` : input.required ? "required" : "",
          oninput: () => {
            state.values[input.name] = field.value;
          },
        });
        return el(
          "label",
          { class: "field" },
          el("span", {}, el("code", {}, input.name), input.required ? el("span", { class: "required" }, " required") : null),
          field,
          input.description ? el("span", { class: "muted small" }, input.description) : null,
        );
      });
      const result = el("div", {});
      const launch = actionButton(
        "Start run",
        async () => {
          const missing = missingRequiredInputs(declared, state.values);
          if (missing.length) throw new Error(`Fill in: ${missing.join(", ")}`);
          await ctx.api.startRun({
            projectId: state.project.id,
            workflow: w.name,
            message: message.value,
            inputs: collectSuppliedInputs(declared, state.values),
          });
          replace(result, notice("Started. It appears under Runs in a few seconds.", "ok"), el("a", { href: "#/runs" }, "Go to Runs"));
        },
        { tone: "primary" },
      );
      replace(
        form,
        el("h2", {}, w.name),
        w.description ? el("p", { class: "description" }, w.description) : null,
        el("label", { class: "field" }, el("span", {}, "Message"), message),
        fields.length ? el("fieldset", {}, el("legend", {}, "Inputs"), fields) : null,
        el("div", { class: "row" }, launch, el("span", { class: "muted small" }, `in ${state.project?.name ?? ""}`)),
        result,
      );
    }

    async function pickProject(id) {
      state.project = state.projects.find((p) => p.id === id) ?? state.projects[0] ?? null;
      state.workflow = null;
      replace(workflowList, notice("Loading workflows..."));
      renderForm();
      if (!state.project) return;
      try {
        const listing = await ctx.api.workflows(state.project.default_cwd);
        state.workflows = (listing.workflows ?? []).map((entry) => entry.workflow).sort((a, b) => a.name.localeCompare(b.name));
        if (params.workflow) state.workflow = state.workflows.find((w) => w.name === params.workflow) ?? null;
        renderWorkflows();
        renderForm();
      } catch (error) {
        replace(workflowList, notice(error.message, "error"));
      }
    }

    filter.addEventListener("input", renderWorkflows);
    projectSelect.addEventListener("change", () => pickProject(projectSelect.value));
    ctx.api
      .projects()
      .then((projects) => {
        state.projects = Array.isArray(projects) ? projects : [];
        const first = params.project ?? state.projects[0]?.id;
        replace(projectSelect, state.projects.map((p) => el("option", { value: p.id, selected: p.id === first }, p.name)));
        pickProject(first);
      })
      .catch((error) => replace(workflowList, notice(error.message, "error")));
    return () => {};
  },
};
