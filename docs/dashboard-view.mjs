import { createActionRenderer } from "./dashboard-view/actions.mjs";
import { createDetailRenderers } from "./dashboard-view/details.mjs";
import { createDom } from "./dashboard-view/dom.mjs";
import { createPbiRenderer } from "./dashboard-view/pbi.mjs";
import { createRepositoryRenderer } from "./dashboard-view/repository.mjs";
import { createSummaryRenderer } from "./dashboard-view/summary.mjs";

export function createDashboardView({
  document,
  summaryOutput,
  dashboardOutput,
  actionLog,
  actionsOutput,
  runAction,
}) {
  const dom = createDom(document);
  const details = createDetailRenderers(dom);
  const renderPbi = createPbiRenderer(dom, details, runAction);
  const renderRepository = createRepositoryRenderer(dom, renderPbi, runAction);
  const renderSummary = createSummaryRenderer(dom, summaryOutput);
  const renderActions = createActionRenderer(dom, actionLog, actionsOutput);
  const { element } = dom;
  function renderProject(state) {
    const section = element("section", undefined, "project-meta");
    section.append(element("h2", state.name || state.project_id || "Project"));
    if (state.updated_at) {
      section.append(element("div", `Updated: ${state.updated_at}`, "muted"));
    }
    return section;
  }

  function render(state) {
    renderSummary(state.counts || {}, state.scheduler);
    dashboardOutput.replaceChildren();
    dashboardOutput.append(renderProject(state));
    const repositories = state.repositories || [];
    if (repositories.length === 0) {
      dashboardOutput.append(
        element("div", "The service has no linked repositories yet.", "empty"),
      );
    } else {
      repositories.forEach((repository) => {
        dashboardOutput.append(renderRepository(repository));
      });
    }
    renderActions(state.actions || []);
  }

  return { render };
}
