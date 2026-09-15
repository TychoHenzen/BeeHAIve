export function createSummaryRenderer(dom, summaryOutput) {
  const { element } = dom;
  function renderSummary(counts, scheduler) {
    summaryOutput.replaceChildren();
    const fields = [
      ["Projects", counts.projects],
      ["Repositories", `${counts.active_repositories || 0} / ${counts.repositories || 0}`],
      ["PBIs", counts.pbis],
      ["Subtasks", counts.subtasks],
      ["Writers", counts.writers],
      ["Readers", counts.readers],
      ["Active runs", counts.active_runs],
      ["Waiting for operator", counts.awaiting_operator_runs],
      ["Failed runs", counts.failed_runs],
      ["Completed runs", counts.completed_runs],
    ];
    if (scheduler && typeof scheduler === "object") {
      const enabled = scheduler.enabled === true
        ? "enabled"
        : scheduler.enabled === false
          ? "disabled"
          : "unavailable";
      const running = scheduler.running === true
        ? "running"
        : scheduler.running === false
          ? "stopped"
          : "unavailable";
      const display = (value) => value === undefined || value === null ? "Unavailable" : value;
      fields.push(
        ["Scheduler", `${enabled}, ${running}`],
        ["Worker capacity", `${display(scheduler.active_workers)} / ${display(scheduler.max_concurrency)}`],
      );
      if (scheduler.last_poll_at) fields.push(["Last poll", scheduler.last_poll_at]);
      if (scheduler.last_error) fields.push(["Scheduler error", scheduler.last_error]);
    }
    fields.forEach(([label, value]) => {
      const card = element("div", undefined, "card");
      card.append(element("div", label, "label"));
      card.append(element("div", value ?? 0, "value"));
      summaryOutput.append(card);
    });
  }
  return renderSummary;
}
