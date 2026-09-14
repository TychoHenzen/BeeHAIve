export function createActionRenderer(dom, actionLog, actionsOutput) {
  const { element } = dom;
  function renderActions(actions) {
    actionLog.hidden = actions.length === 0;
    actionsOutput.replaceChildren();
    actions.forEach((action) => {
      const row = element("div", undefined, "action-row");
      row.append(element("span", action.kind, "mono"));
      row.append(element("span", action.status, `pill ${action.status}`));
      if (action.repository) row.append(element("span", action.repository, "muted"));
      if (action.error) row.append(element("span", action.error, "status failure"));
      actionsOutput.append(row);
    });
  }
  return renderActions;
}
