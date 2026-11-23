// The projects Archon knows, with where they live and a way to start a run.
import { el, notice, replace } from "../dom.js";

export default {
  id: "projects",
  title: "Projects",
  nav: true,
  match: (path) => (path === "/projects" ? {} : null),
  mount(root, _params, ctx) {
    const list = el("div", { class: "project-list" });
    replace(root, el("div", { class: "page-head" }, el("h1", {}, "Projects")), list);
    ctx.api
      .projects()
      .then((projects) => {
        const rows = Array.isArray(projects) ? projects : [];
        replace(
          list,
          rows.length ? null : notice("Archon has no projects yet. Add one with archon serve's console or the CLI."),
          rows.map((p) =>
            el(
              "article",
              { class: "panel project" },
              el("header", { class: "item-head" }, el("strong", {}, p.name), el("span", { class: "pill" }, p.kind ?? "repo")),
              el(
                "dl",
                { class: "facts" },
                el("dt", {}, "Folder"),
                el("dd", {}, el("code", {}, p.default_cwd ?? "")),
                p.repository_url ? [el("dt", {}, "Repository"), el("dd", {}, el("a", { href: p.repository_url, target: "_blank", rel: "noreferrer" }, p.repository_url))] : null,
                p.default_branch ? [el("dt", {}, "Default branch"), el("dd", {}, p.default_branch)] : null,
                el("dt", {}, "Id"),
                el("dd", {}, el("code", {}, p.id)),
              ),
              el(
                "div",
                { class: "row" },
                el("a", { class: "button", href: `#/launch?project=${encodeURIComponent(p.id)}` }, "Launch a run"),
                el("a", { class: "button", href: `#/triggers?project=${encodeURIComponent(p.id)}` }, "Schedule a queue"),
              ),
            ),
          ),
        );
      })
      .catch((error) => replace(list, notice(`Archon did not answer: ${error.message}`, "error")));
    return () => {};
  },
};
