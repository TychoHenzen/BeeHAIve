// Every run across every project, filtered by status, project and text.
import { ago, badge, el, notice, replace, runDuration, short } from "../dom.js";
import { attentionText, needsPerson, STATUS_FILTERS } from "../model.js";

const PAGE = 50;
const POLL_MS = 15000;

export default {
  id: "runs",
  title: "Runs",
  nav: true,
  match: (path) => (path === "/" || path === "/runs" ? {} : null),
  mount(root, _params, ctx) {
    const state = { status: "all", search: "", project: "", limit: PAGE, projects: [] };
    const chips = el("div", { class: "chips", role: "group", "aria-label": "Status" });
    const search = el("input", { type: "search", placeholder: "Search workflow, project, run id...", "aria-label": "Search runs" });
    const projectSelect = el("select", { "aria-label": "Project" });
    const list = el("div", { class: "run-list", "aria-live": "polite" });
    const more = el("div", { class: "more" });
    replace(
      root,
      el("div", { class: "page-head" }, el("h1", {}, "Runs"), el("div", { class: "filters" }, chips, projectSelect, search)),
      list,
      more,
    );

    let searchTimer = null;
    search.addEventListener("input", () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        state.search = search.value.trim();
        state.limit = PAGE;
        load();
      }, 250);
    });
    projectSelect.addEventListener("change", () => {
      state.project = projectSelect.value;
      state.limit = PAGE;
      load();
    });

    function renderChips(counts) {
      replace(
        chips,
        STATUS_FILTERS.map((status) =>
          el(
            "button",
            {
              type: "button",
              class: `chip${state.status === status ? " active" : ""}`,
              "aria-pressed": state.status === status ? "true" : "false",
              onclick: () => {
                state.status = status;
                state.limit = PAGE;
                load();
              },
            },
            status,
            el("span", { class: "count" }, String(counts?.[status] ?? 0)),
          ),
        ),
      );
    }

    function renderProjects() {
      replace(
        projectSelect,
        el("option", { value: "" }, "All projects"),
        state.projects.map((p) => el("option", { value: p.id, selected: p.id === state.project }, p.name)),
      );
    }

    function row(run) {
      const attention = needsPerson(run.attention) ? el("div", { class: "run-attention" }, attentionText(run.attention)) : null;
      return el(
        "a",
        { class: "run-row", href: `#/runs/${run.id}` },
        badge(run),
        el(
          "div",
          { class: "run-main" },
          el("div", { class: "run-title" }, el("strong", {}, run.workflow_name), el("span", { class: "muted" }, run.codebase_name ?? "")),
          run.user_message ? el("div", { class: "run-message muted" }, run.user_message) : null,
          attention,
        ),
        el("div", { class: "run-meta muted small" }, ago(run.started_at), el("div", {}, runDuration(run)), el("code", {}, short(run.id))),
      );
    }

    let loading = false;
    async function load() {
      if (loading) return;
      loading = true;
      try {
        const page = await ctx.api.runs({
          status: state.status === "all" ? "" : state.status,
          search: state.search,
          codebaseId: state.project,
          limit: state.limit,
        });
        renderChips(page.counts);
        const runs = page.runs ?? [];
        replace(list, runs.length ? runs.map(row) : notice("No runs match."));
        replace(
          more,
          runs.length < (page.total ?? 0) && runs.length >= state.limit
            ? el("button", { type: "button", onclick: () => ((state.limit += PAGE), load()) }, "Show more")
            : null,
        );
      } catch (error) {
        replace(list, notice(`Archon did not answer: ${error.message}`, "error"));
      } finally {
        loading = false;
      }
    }

    ctx.api
      .projects()
      .then((projects) => {
        state.projects = Array.isArray(projects) ? projects : [];
        renderProjects();
      })
      .catch(() => renderProjects());
    renderChips({});
    load();
    const unsubscribe = ctx.live.subscribe(load);
    const timer = setInterval(load, POLL_MS);
    return () => {
      unsubscribe();
      clearInterval(timer);
    };
  },
};
