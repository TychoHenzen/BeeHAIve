export function createDashboardView({
  document,
  summaryOutput,
  dashboardOutput,
  actionLog,
  actionsOutput,
  runAction,
}) {
  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }

  function render(state) {
    renderSummary(state.counts || {});
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

  function renderProject(state) {
    const section = element("section", undefined, "project-meta");
    section.append(element("h2", state.name || state.project_id || "Project"));
    if (state.updated_at) {
      section.append(element("div", `Updated: ${state.updated_at}`, "muted"));
    }
    return section;
  }

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

  function renderRepository(repository) {
    const section = element("section", undefined, "repo");
    const header = element("div", undefined, "repo-header");
    const name = element("h2", repository.name || "Unknown repository");
    name.append(element("span", repository.active ? " active" : " inactive", "muted"));
    const writer = repository.writer || { status: "idle" };
    const writerText = writer.status === "active"
      ? `Writer active on PBI #${writer.current_pbi}`
      : "Writer idle";
    header.append(name, element("div", writerText, `writer ${writer.status}`));
    section.append(header);
    if (repository.active && writer.status !== "active") {
      const controls = element("div", undefined, "actions");
      const start = element("button", "Start writer", "secondary");
      start.type = "button";
      start.addEventListener("click", () => runAction({ action: "start", repository: repository.name }));
      controls.append(start);
      section.append(controls);
    }
    const body = element("div", undefined, "repo-body");
    const pbis = repository.pbis || [];
    if (pbis.length === 0) body.append(element("div", "No PBIs in this repository.", "empty"));
    pbis.forEach((pbi) => body.append(renderPbi(repository.name, pbi)));
    section.append(body);
    return section;
  }

  function renderPbi(repository, pbi) {
    const card = element("article", undefined, "pbi");
    if (!card.dataset) card.dataset = {};
    card.dataset.repository = String(repository || "");
    card.dataset.pbiNumber = String(pbi.number ?? "");
    card.dataset.runId = String(pbi.run_id ?? "");
    card.dataset.attempt = String(pbi.attempt ?? "");
    const header = element("div", undefined, "pbi-header");
    const title = element("div");
    title.append(element("div", `${pbi.id || "PBI"} · #${pbi.number}`, "mono"));
    title.append(element("div", pbi.title || "Untitled PBI", "pbi-title"));
    const status = element("span", pbi.status || "pending", `pill ${pbi.status || ""}`);
    header.append(title, status);
    card.append(header);
    if (pbi.archived) {
      card.append(element("div", "Archived: completion evidence verified", "muted"));
    }
    if (pbi.source_url) card.append(renderEvidenceLink("Source issue", pbi.source_url));
    if (pbi.branch) card.append(element("div", `Branch: ${pbi.branch}`, "muted"));
    if (pbi.pull_request_url) card.append(renderEvidenceLink("Pull request", pbi.pull_request_url));
    card.append(renderProgress(pbi.stage_progress || []));
    const details = element("div", undefined, "details");
    if (pbi.task_contract) details.append(renderTaskContract(pbi.task_contract));
    if (pbi.task_result) details.append(renderTaskResult(pbi.task_result));
    details.append(renderListSection("Subtasks", pbi.subtasks, (item) => `${item.id || "subtask"}: ${item.title || ""}`));
    if (pbi.planning_status) card.append(element("div", `Project status: ${pbi.planning_status}`, "muted"));
    details.append(renderListSection("Pull requests", pbi.pull_requests, (item) => pullRequestLabel(item)));
    if (pbi.checks && typeof pbi.checks === "object") details.append(renderChecks(pbi.checks));
    details.append(renderListSection("Readers", pbi.readers, (item) => `${item.pull_request ? `PR #${item.pull_request} ` : ""}${item.id || item.name || "reader"}: ${item.status || "pending"}`));
    details.append(renderReviewers(pbi.reviewers || {}));
    details.append(renderEscalation(pbi.escalation, pbi.escalation_log));
    details.append(renderActivity(pbi.activity || []));
    card.append(details);
    if (pbi.last_error) card.append(element("p", `Failure: ${pbi.last_error}`, "status failure"));
    if (pbi.result) card.append(element("p", `Result: ${pbi.result}`, "status success"));
    if (pbi.status !== "active" || !pbi.run_id) return card;
    const controls = element("div", undefined, "actions");
    const stop = element("button", "Stop", "danger");
    stop.type = "button";
    stop.addEventListener("click", () => runAction({ action: "stop", run_id: pbi.run_id, repository, pbi_number: pbi.number }));
    controls.append(stop);
    const approve = element("button", "Record approval", "secondary");
    approve.type = "button";
    approve.addEventListener("click", () => runAction({ action: "approve", repository, pbi_number: pbi.number, run_id: pbi.run_id }));
    controls.append(approve);
    const clarify = element("button", "Request clarification", "secondary");
    clarify.type = "button";
    clarify.addEventListener("click", () => {
      const message = window.prompt("Clarification for the active run:");
      if (message && message.trim()) runAction({ action: "clarify", repository, pbi_number: pbi.number, run_id: pbi.run_id, clarification: message.trim() });
    });
    controls.append(clarify);
    card.append(controls);
    return card;
  }

  function renderEvidenceLink(label, value) {
    const raw = String(value);
    if (!/^https?:\/\/[^\s]+$/i.test(raw)) {
      return element("div", `${label}: ${raw}`, "muted");
    }
    const container = element("div", undefined, "muted");
    const link = element("a", raw);
    link.href = raw;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    container.append(element("span", `${label}: `), link);
    return container;
  }

  function renderTaskContract(contract) {
    const section = element("section");
    section.append(element("h3", "Task contract"));
    section.append(element("div", `${contract.contract_id || "unknown"} v${contract.version ?? "?"} · ${contract.step_id || "unknown"}`, "muted"));
    if (contract.inputs && typeof contract.inputs === "object") section.append(element("div", `Inputs: ${JSON.stringify(contract.inputs)}`, "muted"));
    if (Array.isArray(contract.capabilities)) section.append(element("div", `Capabilities: ${contract.capabilities.join(", ") || "none"}`, "muted"));
    if (Array.isArray(contract.allowed_outcomes)) section.append(element("div", `Allowed outcomes: ${contract.allowed_outcomes.join(", ") || "none"}`, "muted"));
    if (Array.isArray(contract.required_evidence)) section.append(element("div", `Required evidence: ${contract.required_evidence.join(", ") || "none"}`, "muted"));
    if (Array.isArray(contract.required_artifacts)) {
      const artifacts = element("ul");
      contract.required_artifacts.forEach((artifact) => {
        const id = artifact?.id || "unknown";
        const description = artifact?.description ? `: ${artifact.description}` : "";
        const required = artifact?.required === false ? " (optional)" : " (required)";
        artifacts.append(element("li", `${id}${description}${required}`, "muted"));
      });
      section.append(element("div", "Required artifacts:"), artifacts);
    }
    return section;
  }

  function renderTaskResult(result) {
    const section = element("section");
    section.append(element("h3", `Task outcome: ${result.outcome || "unknown"}`));
    if (result.question) section.append(element("div", `Question: ${result.question}`, "status pending"));
    if (result.required_action) section.append(element("div", `Required action: ${result.required_action}`, "status pending"));
    if (result.answer) section.append(element("div", `Answer: ${result.answer}`, "muted"));
    if (result.validation_reason) section.append(element("div", `Validation: ${result.validation_reason}`, "status failure"));
    if (result.evidence && typeof result.evidence === "object") section.append(element("div", `Evidence: ${JSON.stringify(result.evidence)}`, "muted"));
    if (Array.isArray(result.artifact_refs)) section.append(element("div", `Artifacts: ${JSON.stringify(result.artifact_refs)}`, "muted"));
    return section;
  }

  function renderProgress(progress) {
    const list = element("ol", undefined, "progress");
    progress.forEach((stage) => list.append(element("li", stage.label, stage.status)));
    return list;
  }

  function renderListSection(title, values, format) {
    const section = element("section");
    section.append(element("h3", title));
    const list = values || [];
    if (list.length === 0) section.append(element("div", "None", "muted"));
    else {
      const items = element("ul");
      list.forEach((value) => items.append(element("li", format(value), "muted")));
      section.append(items);
    }
    return section;
  }

  function pullRequestLabel(item) {
    const evidence = [
      typeof item.url === "string" ? item.url : "",
      item.source_branch
        ? `branch ${item.source_branch} (${item.source_branch_state || "unknown"})`
        : "",
    ].filter(Boolean);
    const suffix = evidence.length ? ` (${evidence.join(", ")})` : "";
    if (item.merged === true) return `#${item.number}: merged${suffix}`;
    const state = typeof item.state === "string" ? item.state.toLowerCase() : "";
    const decision = item.review_decision || "";
    if (state === "open" && !decision) return `#${item.number}: open, review pending${suffix}`;
    if (state && decision) return `#${item.number}: ${state}, ${decision}${suffix}`;
    return `#${item.number}: ${state || decision || "review pending"}${suffix}`;
  }

  function renderChecks(checks) {
    const section = element("section");
    const verdict = checks.verdict || "unproven";
    section.append(element("h3", "Checks"));
    section.append(element("div", `Overall: ${verdict}`, checkStatusClass(verdict)));
    (checks.pull_requests || []).forEach((pullRequest) => {
      const head = pullRequest.head_sha || "unknown";
      section.append(element("div", `PR #${pullRequest.number || "?"}: ${pullRequest.verdict || "unproven"}; head ${head}`, "muted"));
      const contexts = pullRequest.contexts || [];
      if (pullRequest.error) section.append(element("div", `Unproven: ${pullRequest.error}`, "status pending"));
      if (contexts.length === 0) return;
      const list = element("ul");
      contexts.forEach((context) => {
        const detail = [
          context.name || "unknown",
          context.verdict || "unproven",
          context.required === true ? "required" : context.required === false ? "optional" : "requiredness unknown",
          context.state || context.status || "state unknown",
          context.conclusion || "",
          context.url || "",
        ].filter(Boolean).join("; ");
        list.append(element("li", detail, checkStatusClass(context.verdict)));
      });
      section.append(list);
    });
    return section;
  }

  function checkStatusClass(verdict) {
    if (verdict === "passing") return "status success";
    if (verdict === "blocking") return "status failure";
    return "status pending";
  }

  function renderReviewers(reviewers) {
    const values = Object.entries(reviewers).map(([name, result]) => ({ name, ...result }));
    return renderListSection("Reviewer results", values, (item) => `${item.name}: ${item.status || "pending"}${item.comment ? ` · ${item.comment}` : ""}`);
  }

  function renderEscalation(escalation, log) {
    const section = element("section");
    section.append(element("h3", "Escalation"));
    const current = escalation || { current: 0, consecutive: 0 };
    const tier = current.current_tier || current.current;
    section.append(element("div", `Tier ${tier}; ${current.consecutive} consecutive failures`, "muted"));
    if ((log || []).length) {
      const list = element("ul");
      log.forEach((entry) => list.append(element("li", JSON.stringify(entry), "muted")));
      section.append(list);
    }
    return section;
  }

  function renderActivity(activity) {
    const section = element("section");
    section.append(element("h3", "Activity"));
    if (!activity.length) section.append(element("div", "None", "muted"));
    else {
      const list = element("ul");
      activity.slice().reverse().forEach((event) => {
        const transition = event.from_stage && event.to_stage ? ` ${event.from_stage} -> ${event.to_stage}` : "";
        const timestamp = event.created_at || event.time || "";
        const description = event.action || event.type || "event";
        list.append(element("li", `${timestamp} ${description}${transition}`, "muted"));
      });
      section.append(list);
    }
    return section;
  }

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

  return { render };
}
