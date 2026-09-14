export function createSummaryRenderer(dom, summaryOutput) {
  const { element } = dom;
  function renderSummary(counts) {
    summaryOutput.replaceChildren();
    const fields = [
      ["Projects", counts.projects],
      ["Repositories", `${counts.active_repositories || 0} / ${counts.repositories || 0}`],
      ["PBIs", counts.pbis],
      ["Subtasks", counts.subtasks],
      ["Writers", counts.writers],
      ["Readers", counts.readers],
      ["Active runs", counts.active_runs],
      ["Failed runs", counts.failed_runs],
      ["Completed runs", counts.completed_runs],
    ];
    fields.forEach(([label, value]) => {
      const card = element("div", undefined, "card");
      card.append(element("div", label, "label"));
      card.append(element("div", value ?? 0, "value"));
      summaryOutput.append(card);
    });
  }
  return renderSummary;
}
