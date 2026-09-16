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
      if (action.result !== null && action.result !== undefined) {
        const result = typeof action.result === "string"
          ? action.result
          : JSON.stringify(action.result) || "";
        row.append(element("span", `Result: ${result.slice(0, 4_000)}`, "muted"));
      }
      if (action.error) {
        row.append(element("span", String(action.error).slice(0, 4_000), "status failure"));
      }
      actionsOutput.append(row);
    });
  }
  return renderActions;
}
