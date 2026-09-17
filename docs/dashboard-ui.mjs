const STAGES = [
  ["backlog", "Backlog"],
  ["refine", "Refine"],
  ["implement", "Implement"],
  ["review", "Review"],
  ["pull_request", "Pull request"],
  ["merge", "Merged"],
];

const FILTERS = [
  ["all", "All"],
  ["claimable", "Claimable"],
  ["blocked", "Blocked"],
  ["archived", "Archived"],
];
const TERMINAL_PLANNING_STATUSES = new Set(["done", "completed", "closed", "merged"]);

const ACTION_LABELS = {
  advance: "Moved to implementation",
  autonomous_complete: "Autonomous lifecycle completed",
  autonomous_start: "Autonomous lifecycle started",
  answer_question: "Operator answer recorded",
  approve: "Approval recorded",
  clarify: "Clarification requested",
  commit_push: "Delivery retried",
  deliver: "Delivery completed",
  retry: "Work retried",
  start: "Work started",
  stop: "Work stopped",
};

function text(value, fallback = "Unavailable") {
  return value === undefined || value === null || value === "" ? fallback : String(value);
}

function list(value) {
  return Array.isArray(value) ? value : [];
}

function map(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function stageId(pbi) {
  const raw = String(pbi.stage || "backlog");
  return STAGES.some(([id]) => id === raw) ? raw : "backlog";
}

function stageLabel(pbi) {
  if (pbi.stage_label) return String(pbi.stage_label);
  return STAGES.find(([id]) => id === stageId(pbi))?.[1] || "Backlog";
}

function isCompleted(pbi) {
  return pbi.status === "completed"
    || pbi.archived === true
    || TERMINAL_PLANNING_STATUSES.has(String(pbi.planning_status || "").toLowerCase());
}

function needsAttention(pbi) {
  return ["awaiting_operator", "failed"].includes(pbi.status)
    || map(pbi.checks).verdict === "blocking";
}

function readiness(pbi) {
  if (pbi.status === "awaiting_operator") return "Awaiting operator";
  if (pbi.delivery?.status === "push_failed") return "Push blocked · retry kept";
  if (pbi.status === "failed") return "Blocked";
  if (isCompleted(pbi)) return "Delivered";
  if (pbi.status === "active") return "Running";
  if (map(pbi.dependency_readiness).status === "blocked") return "Blocked by dependency";
  if (pbi.claimable) return "Claimable";
  if (stageId(pbi) === "pull_request") {
    return map(pbi.checks).verdict === "unproven" ? "Checks unproven" : "Review pending";
  }
  return "Waiting";
}

function agentLabel(item) {
  const session = map(item.pbi.agent_session);
  const writer = map(item.repositoryState?.writer);
  if (item.pbi.autonomous_status === "running" || (item.pbi.status === "active" && item.pbi.run_id && !session.worker_id && writer.status !== "active")) return "autonomous";
  if (session.worker_id) return session.worker_id;
  if (writer.status === "active" && writer.current_pbi === item.pbi.number) return "writer";
  return "-";
}

function isAgentActive(item) {
  const writer = map(item.repositoryState?.writer);
  return (['active', 'awaiting_operator'].includes(item.pbi.status) && item.pbi.run_id)
    || (writer.status === "active" && writer.current_pbi === item.pbi.number);
}

function activityDetail(action) {
  const kind = String(action.kind || "");
  const request = map(action.request);
  const result = map(action.result);
  const run = map(result.run);
  if (action.error) return `Failure: ${action.error}`;
  if (kind.startsWith("skill:")) return text(result.summary, "Skill context completed.");
  if (kind === "scheduler_configure") {
    return `${request.enabled === true ? "Polling enabled" : "Polling disabled"} · ${text(request.max_concurrency, "?")} workers · every ${text(request.poll_interval_seconds, "?")} seconds.`;
  }
  if (kind === "stop") return `Reason: ${text(request.reason, "Stopped by operator")}`;
  if (kind === "retry") return `Resumed ${text(request.repository, "the repository")}#${text(request.pbi_number, "?")} at attempt ${text(run.attempt, "?")}.`;
  if (kind === "start") return `Started ${text(request.repository, "the repository")}#${text(request.pbi_number, "?")} at ${text(run.stage, "the current stage")}.`;
  if (kind === "autonomous_start") return `Selected ${text(request.repository, "the repository")}#${text(request.pbi_number, "?")} for the autonomous lifecycle.`;
  if (kind === "autonomous_complete") return text(result.summary, "All autonomous lifecycle handoffs completed.");
  if (kind === "answer_question") return `Answered operator question ${text(request.question_id, "the pending question")} and resumed the run.`;
  if (kind === "clarify") return "Clarification was sent to the active worker.";
  if (kind === "synchronize" || kind === "sync") return "Read the provider and refreshed the local read model.";
  if (run.run_id) return `Run ${run.run_id} · stage ${text(run.stage, "unknown")}.`;
  return text(result.message, "No additional detail recorded.");
}

function allItems(state) {
  return list(state.repositories).flatMap((repository) =>
    list(repository.pbis).map((pbi) => ({
      repository: text(repository.name, "Unknown repository"),
      project: text(
        repository.project || pbi.project || state.name || state.project_id,
        "Project",
      ),
      repositoryState: repository,
      pbi,
    })),
  );
}

function actionSpec(item, demo) {
  const { repository, pbi } = item;
  const identity = { repository, pbi_number: pbi.number, run_id: pbi.run_id };
  if (pbi.autonomous_status === "blocked") {
    return {
      label: "Retry lifecycle",
      testid: "retry-lifecycle",
      autonomous: true,
      payload: { repository, pbi_number: pbi.number },
    };
  }
  if (pbi.autonomous_status === "running") return null;
  if (map(pbi.delivery).retry_available && pbi.run_id && pbi.active !== false) {
    return {
      label: "Retry delivery",
      testid: "retry-delivery",
      payload: { action: "commit_push", ...identity },
    };
  }
  if (pbi.status === "failed") {
    if (pbi.claimable && pbi.run_id) return { label: "Retry work", testid: "retry-work", payload: { action: "retry", ...identity } };
  }
  if (pbi.claimable && !pbi.run_id && !isCompleted(pbi)) return { label: "Start work", testid: "start-work", payload: { action: "start", repository, pbi_number: pbi.number } };
  if (!demo) return null;
  if (demo && stageId(pbi) === "pull_request" && pbi.run_id === null) return { label: "Approve review", testid: "approve-review", payload: { action: "approve", ...identity } };
  if (pbi.status !== "active" || !pbi.run_id) return null;
  if (stageId(pbi) === "refine") return null;
  if (demo && stageId(pbi) === "implement") return { label: "Send to review", testid: "send-to-review", payload: { action: "approve", ...identity } };
  if (demo && stageId(pbi) === "merge") return { label: "Merge change", testid: "merge-change", payload: { action: "merge", ...identity } };
  if (stageId(pbi) === "implement") return { label: "Record approval", testid: "approve-step", payload: { action: "approve", ...identity } };
  return null;
}

export function createDashboardUi({
  document,
  projectSwitcher,
  refreshAge,
  metrics,
  attention,
  filters,
  queueTable,
  agentsOutput,
  activityOutput,
  deliveriesOutput,
  graphOutput,
  workflowSelector,
  settingsWorkflowInput,
  projectsOutput,
  schedulerCard,
  detailsPane,
  detailsOutput,
  runAction,
  runAutonomous,
  onQueueArchive = () => {},
  initialFilter = "all",
  demo = false,
}) {
  let currentState = null;
  let currentFilter = initialFilter;
  let currentProjectFilter = "all";
  let selectedKey = "";
  let currentPage = "mission";

  function element(tag, value, className) {
    const node = document.createElement(tag);
    if (value !== undefined) node.textContent = String(value);
    if (className) node.className = className;
    return node;
  }

  function actionButton(label, spec, className = "") {
    const node = element("button", label, className);
    node.type = "button";
    node.dataset.testid = spec.testid;
    node.addEventListener("click", (event) => {
      event.stopPropagation();
      void (spec.autonomous ? runAutonomous(spec.payload) : runAction(spec.payload));
    });
    return node;
  }

  const autonomousLabel = demo ? "Run placeholder lifecycle" : "Run lifecycle on server";

  function filterItems(items, filter) {
    return items.filter(({ pbi }) => {
      if (filter === "claimable") return pbi.claimable && !isCompleted(pbi);
      if (filter === "blocked") return needsAttention(pbi);
      if (filter === "archived") return isCompleted(pbi);
      return true;
    });
  }

  function renderProjectSwitcher(state) {
    projectSwitcher.replaceChildren();
    const projectNames = list(state.projects).length
      ? list(state.projects)
      : [state.name || state.project_id];
    if (currentProjectFilter !== "all" && !projectNames.includes(currentProjectFilter)) {
      currentProjectFilter = "all";
    }
    const all = element("button", "All projects", "project-filter");
    all.type = "button";
    all.setAttribute("aria-pressed", String(currentProjectFilter === "all"));
    all.addEventListener("click", () => {
      currentProjectFilter = "all";
      render(currentState);
    });
    projectSwitcher.append(all);
    projectNames.filter(Boolean).forEach((name) => {
      const project = element("button", name, "project-filter");
      project.type = "button";
      project.setAttribute("aria-pressed", String(currentProjectFilter === name));
      project.addEventListener("click", () => {
        currentProjectFilter = name;
        render(currentState);
      });
      projectSwitcher.append(project);
    });
  }

  function renderMetrics(items) {
    const values = [
      ["Claimable", items.filter(({ pbi }) => pbi.claimable && !isCompleted(pbi)).length, ""],
      ["Active runs", items.filter(({ pbi }) => pbi.status === "active").length, "active"],
      ["Blocked", items.filter(({ pbi }) => pbi.status === "failed" || map(pbi.dependency_readiness).status === "blocked").length, "warning"],
      ["Delivered today", items.filter(({ pbi }) => isCompleted(pbi)).length, "done"],
      ["Unproven checks", items.filter(({ pbi }) => map(pbi.checks).verdict === "unproven").length, ""],
    ];
    metrics.replaceChildren();
    values.forEach(([label, value, className]) => {
      const card = element("div", undefined, `metric ${className}`);
      card.append(element("span", label), element("strong", value));
      metrics.append(card);
    });
  }

  function renderAttention(items) {
    const pending = items.filter(({ pbi }) => list(pbi.operator_questions).some((question) => question.status === "pending"));
    attention.replaceChildren();
    attention.hidden = pending.length === 0;
    if (!pending.length) return;
    const item = pending[0];
    const question = list(item.pbi.operator_questions).find((value) => value.status === "pending");
    const heading = element("div", undefined, "attention-heading");
    heading.append(element("span", "", "status-dot"), element("h2", "Needs you"), element("span", `${pending.length} agent${pending.length === 1 ? "" : "s"} waiting`, "tag accent"), element("span", "An agent pauses until this is answered. It will not guess.", "attention-note"));
    const content = element("div", undefined, "attention-content");
    const copy = element("div", undefined, "attention-copy");
    copy.append(element("div", `${item.project} · ${item.repository}#${text(item.pbi.number, "?")} · ${agentLabel(item)} · ${text(question?.question_id, "question")}`, "attention-meta"), element("p", text(question?.question, "The worker is waiting for an operator answer.")));
    const controls = element("div", undefined, "attention-actions");
    const answer = element("button", "Answer", "primary");
    answer.type = "button";
    answer.dataset.testid = "answer-attention";
    answer.addEventListener("click", () => openInspector(item));
    const evidence = element("button", "Evidence", "secondary");
    evidence.type = "button";
    evidence.addEventListener("click", () => openInspector(item));
    controls.append(answer, evidence);
    content.append(copy, controls);
    attention.append(heading, content);
  }

  function renderFilters(items) {
    filters.replaceChildren();
    FILTERS.forEach(([id, label]) => {
      const node = element("button", `${label} ${id === "all" ? "" : `(${filterItems(items, id).length})`}`.trim(), "queue-filter");
      node.type = "button";
      node.setAttribute("aria-pressed", String(currentFilter === id));
      node.addEventListener("click", () => {
        currentFilter = id;
        renderQueue(items);
        onQueueArchive(id === "archived");
      });
      filters.append(node);
    });
  }

  function renderProgress(container, pbi) {
    const progress = element("div", undefined, "detail-progress");
    const values = list(pbi.stage_progress).length ? list(pbi.stage_progress) : STAGES.map(([id, label]) => ({ id, label, status: id === stageId(pbi) ? "current" : "pending" }));
    values.forEach((stage) => {
      const step = element("div", undefined, `detail-progress-step ${stage.status}`);
      step.append(element("span", "●"), element("span", "", "line"), element("span", text(stage.label, "Stage")));
      progress.append(step);
    });
    container.append(progress);
  }

  function renderEvidence(container, item) {
    const { pbi } = item;
    const card = element("section", undefined, "detail-card");
    card.append(element("h3", "Evidence"));
    const grid = element("div", undefined, "detail-grid");
    const add = (label, value) => {
      if (value === undefined || value === null || value === "") return;
      const cell = element("p");
      cell.append(element("strong", label), element("span", value));
      grid.append(cell);
    };
    add("Canonical state", map(pbi.canonical_lifecycle).state);
    add("Reason", map(pbi.canonical_lifecycle).reason_code);
    add("Branch", pbi.branch);
    add("Readiness", readiness(pbi));
    add("Checks", map(pbi.checks).verdict);
    add("Failure", pbi.last_error || (pbi.status === "failed" ? "No failure detail was recorded." : null));
    add("State reason", pbi.state_reason);
    add("Required action", map(pbi.canonical_lifecycle).required_action);
    add("Task outcome", map(pbi.task_result).outcome);
    add("Validation", map(pbi.task_result).validation_reason);
    const subtasks = list(pbi.subtasks);
    add("Subtasks", subtasks.length ? `${subtasks.length}${subtasks[0]?.title ? ` · ${subtasks[0].title}` : ""}` : null);
    const escalation = map(pbi.escalation);
    add("Escalation", escalation.current_tier ? `Tier ${escalation.current_tier}` : null);
    if (pbi.result) add("Result", pbi.result);
    const pullRequest = list(pbi.pull_requests)[0];
    if (pullRequest?.url) {
      const cell = element("p");
      cell.append(element("strong", "Pull request"));
      const link = element("a", `#${text(pullRequest.number, "?")}`);
      link.href = pullRequest.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      cell.append(link);
      grid.append(cell);
    }
    card.append(grid);
    container.append(card);
  }

  function renderDelivery(container, item) {
    const { pbi } = item;
    const card = element("section", undefined, "detail-card");
    card.append(element("h3", "Delivery"));
    const grid = element("div", undefined, "detail-grid");
    const add = (label, value) => {
      if (value === undefined || value === null || value === "") return;
      const cell = element("p");
      cell.append(element("strong", label), element("span", value));
      grid.append(cell);
    };
    const delivery = map(pbi.delivery);
    add("Status", delivery.status || (isCompleted(pbi) ? "Delivered" : null));
    add("Commit", delivery.commit_sha);
    add("Delivery branch", delivery.branch);
    add("Result", pbi.result);
    const pullRequest = list(pbi.pull_requests)[0];
    if (pullRequest?.url) {
      const cell = element("p");
      cell.append(element("strong", "Pull request"));
      const link = element("a", `#${text(pullRequest.number, "?")}`);
      link.href = pullRequest.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      cell.append(link);
      grid.append(cell);
    }
    if (grid.children.length) card.append(grid);
    else card.append(element("div", "No delivery evidence yet.", "empty"));
    container.append(card);
  }

  function renderQuestionForm(container, item, question) {
    const form = element("form", undefined, "inline-form");
    form.dataset.testid = "answer-form";
    const label = element("label", "Answer");
    const input = element("textarea");
    input.required = true;
    input.placeholder = "Write the decision the worker needs...";
    input.setAttribute("aria-label", "Answer this question");
    const submit = element("button", "Send and resume run", "primary");
    submit.type = "submit";
    submit.dataset.testid = "answer-question";
    form.append(label, input, submit);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (!input.value.trim()) return;
      void runAction({
        action: "answer_question",
        repository: item.repository,
        pbi_number: item.pbi.number,
        run_id: item.pbi.run_id,
        question_id: question.question_id,
        revision: question.revision,
        answer: input.value.trim(),
      });
      input.disabled = true;
      submit.disabled = true;
    });
    container.append(form);
  }

  function renderClarificationForm(container, item) {
    const form = element("form", undefined, "inline-form");
    const label = element("label", "Clarification");
    const input = element("textarea");
    input.required = true;
    input.placeholder = "Describe the missing context...";
    input.setAttribute("aria-label", "Clarification message");
    const submit = element("button", "Send clarification", "primary");
    submit.type = "submit";
    submit.dataset.testid = "submit-clarification";
    form.append(label, input, submit);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (!input.value.trim()) return;
      void runAction({ action: "clarify", repository: item.repository, pbi_number: item.pbi.number, run_id: item.pbi.run_id, clarification: input.value.trim() });
      input.disabled = true;
      submit.disabled = true;
    });
    container.append(form);
  }

  function renderInspector(item) {
    const { pbi } = item;
    detailsPane.classList.add("open");
    detailsPane.setAttribute("aria-hidden", "false");
    detailsOutput.replaceChildren();
    const header = element("header", undefined, "details-header");
    const copy = element("div", undefined, "details-header-copy");
    copy.append(element("p", `${item.repository}#${text(pbi.number, "?")} · ${text(pbi.run_id, "no active run")}`), element("h2", text(pbi.title, "Untitled work item")), element("span", `${stageLabel(pbi)} · ${readiness(pbi)}`, "tag accent"));
    const close = element("button", "×", "close-pane");
    close.type = "button";
    close.setAttribute("aria-label", "Close run inspector");
    close.addEventListener("click", closeInspector);
    header.append(copy, close);
    detailsOutput.append(header);
    const body = element("div", undefined, "detail-body");
    if (pbi.last_error || pbi.status === "failed") {
      const failure = element("section", undefined, "detail-card failure-summary");
      failure.append(element("h3", "Why it stopped"));
      failure.append(element("p", pbi.last_error || "No failure detail was recorded."));
      body.append(failure);
    }
    const tabs = element("div", undefined, "detail-tabs");
    tabs.setAttribute("role", "tablist");
    const panels = new Map();
    const selectTab = (tabId) => {
      tabs.querySelectorAll(".detail-tab").forEach((tab) => {
        const active = tab.dataset.tab === tabId;
        tab.classList.toggle("active", active);
        tab.setAttribute("aria-selected", String(active));
      });
      panels.forEach((panel, panelId) => { panel.hidden = panelId !== tabId; });
    };
    ["Lifecycle", "Session", "Contract", "Checks", "Delivery"].forEach((label, index) => {
      const tabId = label.toLowerCase();
      const tab = element("button", label, `detail-tab ${index === 0 ? "active" : ""}`);
      tab.type = "button";
      tab.dataset.tab = tabId;
      tab.dataset.testid = `detail-tab-${tabId}`;
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-selected", String(index === 0));
      tab.addEventListener("click", () => selectTab(tabId));
      tabs.append(tab);
    });
    body.append(tabs);
    const lifecyclePanel = element("div", undefined, "detail-panel");
    const sessionPanel = element("div", undefined, "detail-panel");
    const contractPanel = element("div", undefined, "detail-panel");
    const checksPanel = element("div", undefined, "detail-panel");
    const deliveryPanel = element("div", undefined, "detail-panel");
    panels.set("lifecycle", lifecyclePanel);
    panels.set("session", sessionPanel);
    panels.set("contract", contractPanel);
    panels.set("checks", checksPanel);
    panels.set("delivery", deliveryPanel);
    const lifecycle = element("section", undefined, "detail-card");
    lifecycle.append(element("h3", "Lifecycle"));
    renderProgress(lifecycle, pbi);
    const handoffs = list(pbi.autonomous_handoffs);
    if (handoffs.length) {
      const handoffCard = element("section", undefined, "detail-card");
      handoffCard.append(element("h3", "Skill handoffs"));
      const handoffList = element("div", undefined, "agent-events");
      handoffs.forEach((handoff) => {
        const value = map(handoff);
        handoffList.append(element("div", `${text(value.step, "skill")} · ${text(value.status, "unknown")} · ${text(value.summary, "No summary")}`));
      });
      handoffCard.append(handoffList);
      lifecyclePanel.append(handoffCard);
    }
    lifecyclePanel.append(lifecycle);
    const question = list(pbi.operator_questions).find((value) => value.status === "pending");
    if (question) {
      const questionCard = element("section", undefined, "detail-card");
      questionCard.append(element("h3", "Your decision is needed"), element("p", text(question.question, "The worker is waiting for an answer.")));
      const questionEvidence = map(question.evidence);
      if (Object.keys(questionEvidence).length) {
        const evidence = element("div", undefined, "agent-events");
        Object.entries(questionEvidence).slice(0, 8).forEach(([key, value]) => {
          evidence.append(element("div", `${key} = ${text(value)}`));
        });
        questionCard.append(evidence);
      }
      renderQuestionForm(questionCard, item, question);
      lifecyclePanel.append(questionCard);
    }
    renderEvidence(checksPanel, item);
    const session = map(pbi.agent_session);
    if (Object.keys(session).length) {
      const sessionCard = element("section", undefined, "detail-card");
      sessionCard.append(element("h3", "Session"));
      const sessionGrid = element("div", undefined, "detail-grid");
      [["Worker", session.worker_id], ["Task", session.task], ["State", session.state]].forEach(([label, value]) => {
        if (value === undefined || value === null || value === "") return;
        const cell = element("p");
        cell.append(element("strong", label), element("span", value));
        sessionGrid.append(cell);
      });
      sessionCard.append(sessionGrid);
      const events = list(session.events).slice(-8);
      if (events.length) {
        const eventList = element("div", undefined, "agent-events");
        events.forEach((event) => eventList.append(element("div", `${text(event.kind || event.source_type, "event")} · ${text(event.text, "")}`)));
        sessionCard.append(eventList);
      }
      sessionPanel.append(sessionCard);
    } else {
      sessionPanel.append(element("div", "No worker session has been recorded.", "empty"));
    }
    if (pbi.task_contract) {
      const contract = element("section", undefined, "detail-card");
      contract.append(element("h3", "Task contract"), element("p", `${text(pbi.task_contract.contract_id, "Contract")} v${text(pbi.task_contract.version, "?")} · ${text(pbi.task_contract.step_id, "step unavailable")}`));
      const capabilities = list(pbi.task_contract.capabilities);
      if (capabilities.length) contract.append(element("p", `Capabilities: ${capabilities.join(", ")}`));
      contractPanel.append(contract);
    } else {
      contractPanel.append(element("div", "No task contract has been recorded.", "empty"));
    }
    body.append(lifecyclePanel, sessionPanel, contractPanel, checksPanel, deliveryPanel);
    renderDelivery(deliveryPanel, item);
    selectTab("lifecycle");
    const controls = element("div", undefined, "detail-actions");
    if (runAutonomous && pbi.claimable && !pbi.run_id && !isCompleted(pbi)) {
      controls.append(actionButton(autonomousLabel, {
        autonomous: true,
        testid: "run-lifecycle",
        payload: { repository: item.repository, pbi_number: pbi.number },
      }, "primary"));
    }
    const spec = actionSpec(item, demo);
    if (spec) controls.append(actionButton(spec.label, spec, runAutonomous ? "secondary" : "primary"));
    if (pbi.run_id && pbi.autonomous_status !== "running" && ["active", "awaiting_operator"].includes(pbi.status) && pbi.active !== false) {
      const stop = { label: "Stop run", testid: "stop-work", payload: { action: "stop", repository: item.repository, run_id: pbi.run_id } };
      controls.append(actionButton(stop.label, stop, "danger"));
      if (pbi.status === "active") {
        const clarify = element("button", "Request clarification");
        clarify.type = "button";
        clarify.dataset.testid = "clarify-work";
        clarify.addEventListener("click", () => renderClarificationForm(lifecyclePanel, item));
        controls.append(clarify);
      }
    }
    if (controls.children.length) body.append(controls);
    detailsOutput.append(body);
  }

  function openInspector(item) {
    selectedKey = `${item.repository}:${item.pbi.number}`;
    renderInspector(item);
  }

  function closeInspector() {
    selectedKey = "";
    detailsPane.classList.remove("open");
    detailsPane.setAttribute("aria-hidden", "true");
    detailsOutput.replaceChildren();
  }

  function renderRow(item) {
    const { repository, repositoryState, pbi } = item;
    const row = element("article", undefined, `work-item queue-row ${needsAttention(pbi) ? "needs-attention" : ""} ${pbi.claimable ? "needs-action" : ""}`);
    row.dataset.testid = "work-item";
    row.dataset.repository = repository;
    row.dataset.pbiNumber = text(pbi.number, "");
    row.dataset.runId = text(pbi.run_id, "");
    const dot = element("span", "", `row-dot ${pbi.claimable ? "ready" : needsAttention(pbi) ? "blocked" : ""}`);
    const main = element("div", undefined, "row-main");
    main.append(element("div", `#${text(pbi.number, "?")}`, "row-id"), element("h3", text(pbi.title, "Untitled work item"), "row-title"));
    const project = element("span", item.project, "row-project");
    const stage = element("span", stageLabel(pbi), "tag");
    if (needsAttention(pbi)) stage.className = "tag warn";
    const readinessValue = element("span", readiness(pbi), `readiness ${needsAttention(pbi) ? "blocked" : ""}`);
    const agent = element("span", agentLabel(item), "row-agent");
    const controls = element("div", undefined, "row-controls");
    if (runAutonomous && pbi.claimable && !pbi.run_id && !isCompleted(pbi)) {
      controls.append(actionButton(autonomousLabel, {
        autonomous: true,
        testid: "run-lifecycle",
        payload: { repository, pbi_number: pbi.number },
      }, "primary"));
    }
    const spec = actionSpec(item, demo);
    if (spec) controls.append(actionButton(spec.label, spec, runAutonomous ? "secondary" : "primary"));
    const inspect = element("button", pbi.run_id ? "Inspect run" : "Inspect", "inspect");
    inspect.type = "button";
    inspect.dataset.testid = "inspect-work";
    inspect.addEventListener("click", (event) => {
      event.stopPropagation();
      openInspector(item);
    });
    controls.append(inspect);
    row.append(dot, main, project, stage, readinessValue, agent, controls);
    return row;
  }

  function renderQueue(items) {
    renderFilters(items);
    const visible = filterItems(items, currentFilter).sort((left, right) => {
      const rank = (item) => needsAttention(item.pbi) ? 0 : item.pbi.claimable ? 1 : item.pbi.status === "active" ? 2 : isCompleted(item.pbi) ? 4 : 3;
      return rank(left) - rank(right);
    });
    queueTable.replaceChildren();
    const header = element("div", undefined, "queue-row queue-header");
    ["", "Work item", "Project", "Stage", "Readiness", "Agent", ""].forEach((label) => header.append(element("span", label)));
    queueTable.append(header);
    if (!visible.length) {
      queueTable.append(element("div", "Nothing matches this filter.", "empty"));
      return;
    }
    visible.forEach((item) => queueTable.append(renderRow(item)));
    if (selectedKey) {
      const selected = items.find((item) => `${item.repository}:${item.pbi.number}` === selectedKey);
      if (selected) renderInspector(selected);
      else closeInspector();
    }
  }

  function renderAgents(items) {
    agentsOutput.replaceChildren();
    const active = items.filter((item) => isAgentActive(item) || item.pbi.autonomous_status);
    const scheduler = map(currentState?.scheduler);
    const showScheduler = scheduler.enabled === true
      && (scheduler.running === true || scheduler.last_poll_at || scheduler.last_error || Number(scheduler.active_workers) > 0);
    if (!active.length && !showScheduler) {
      agentsOutput.append(element("div", "No active agents.", "empty"));
      return;
    }
    if (showScheduler) {
      const schedulerCard = element("section", undefined, "agent-card");
      const heading = element("div", undefined, "agent-heading");
      heading.append(element("span", "", "status-dot"), element("strong", "scheduler"), element("span", scheduler.running === true ? "Polling" : "Configured", "tag accent"));
      schedulerCard.append(heading, element("p", `Last poll ${text(scheduler.last_poll_at, "not observed")} · ${text(scheduler.active_workers, "0")} of ${text(scheduler.max_concurrency, "?")} workers leased.`));
      if (scheduler.last_started_run_ids?.length) schedulerCard.append(element("p", `Last admitted run: ${scheduler.last_started_run_ids.at(-1)}`, "activity-detail"));
      if (scheduler.last_error) schedulerCard.append(element("p", `Failure: ${scheduler.last_error}`, "activity-detail"));
      agentsOutput.append(schedulerCard);
    }
    active.forEach((item) => {
      const { pbi } = item;
      const stateLabel = pbi.autonomous_status === "running" ? "Running" : pbi.autonomous_status === "blocked" ? "Blocked" : pbi.status === "awaiting_operator" ? "Waiting on you" : pbi.status === "active" ? "Implementing" : "Completed";
      const card = element("section", undefined, `agent-card ${pbi.autonomous_status === "blocked" ? "blocked" : ""}`);
      const heading = element("div", undefined, "agent-heading");
      heading.append(element("span", "", "status-dot"), element("strong", agentLabel(item)), element("span", stateLabel, `tag ${pbi.autonomous_status === "blocked" || pbi.status === "failed" ? "warn" : "accent"}`));
      card.append(heading, element("p", `${item.repository}#${text(pbi.number, "?")} · ${text(pbi.title, "Untitled work item")}`));
      if (pbi.last_error) card.append(element("p", `Failure: ${pbi.last_error}`, "activity-detail"));
      const events = list(map(pbi.agent_session).events).slice(-3);
      if (events.length) {
        const eventList = element("div", undefined, "agent-events");
        events.forEach((event) => eventList.append(element("div", `${text(event.kind || event.source_type, "event")} · ${text(event.text, "")}`)));
        card.append(eventList);
      }
      const controls = element("div", undefined, "actions");
      const inspect = element("button", "Inspect run", "secondary");
      inspect.type = "button";
      inspect.addEventListener("click", () => openInspector(item));
      controls.append(inspect);
      if (pbi.run_id && pbi.autonomous_status !== "running" && pbi.autonomous_status !== "blocked") {
        const stop = { label: "Stop", testid: "stop-work", payload: { action: "stop", repository: item.repository, run_id: pbi.run_id } };
        controls.append(actionButton(stop.label, stop, "danger"));
      }
      card.append(controls);
      agentsOutput.append(card);
    });
  }

  function renderDeliveries(state, items) {
    deliveriesOutput.replaceChildren();
    const records = list(state.recent_deliveries);
    const source = records.length
      ? records.map((record) => ({
        repository: text(record.repository, "Unknown repository"),
        project: text(record.project, state.name || "Project"),
        pbi: map(record.pbi),
      })).filter((item) => currentProjectFilter === "all" || item.project === currentProjectFilter)
      : items.filter(({ pbi }) => isCompleted(pbi) || pbi.delivery || list(pbi.pull_requests).length);
    const delivered = source.slice(-5).reverse();
    if (!delivered.length) {
      deliveriesOutput.append(element("div", "No delivery evidence yet.", "empty"));
      return;
    }
    delivered.forEach((item) => {
      const { pbi } = item;
      const terminal = isCompleted(pbi);
      const card = element("section", undefined, "delivery-card");
      const heading = element("div", undefined, "delivery-heading");
      heading.append(element("strong", text(pbi.delivery?.commit_sha, terminal ? "merged" : "pending")), element("span", readiness(pbi), `tag ${terminal ? "success" : pbi.delivery?.status === "push_failed" ? "warn" : ""}`));
      card.append(heading, element("p", `${item.repository}#${text(pbi.number, "?")} · ${text(pbi.title, "Untitled work item")}`));
      if (pbi.delivery?.evidence) card.append(element("p", pbi.delivery.evidence));
      const pullRequest = list(pbi.pull_requests)[0];
      if (pullRequest?.url) {
        const link = element("a", "Open pull request");
        link.href = pullRequest.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        card.append(link);
      }
      deliveriesOutput.append(card);
    });
  }

  function definitionDocument(definition) {
    const source = map(definition);
    return Object.fromEntries(
      ["workflow_id", "schema_version", "nodes", "edges", "model_policy", "limits", "metadata"]
        .filter((key) => source[key] !== undefined)
        .map((key) => [key, source[key]]),
    );
  }

  function definitionFixtures(definition) {
    const result = {
      outcome: "pass",
      evidence: {},
      artifact_refs: [],
      question: null,
      required_action: null,
      validation_reason: null,
      answer: null,
    };
    return {
      dashboard: Object.fromEntries(list(map(definition).nodes).map((node) => [map(node).node_id, result])),
    };
  }

  function renderGraphPage(state) {
    if (!graphOutput) return;
    graphOutput.replaceChildren();
    const graph = map(state.graph);
    if (!graph.workflow_id) {
      graphOutput.append(element("div", "Choose a workflow ID in Settings to view its graph.", "empty"));
      return;
    }
    const workflowId = String(graph.workflow_id);
    const active = map(graph.active);
    const definitions = list(graph.definitions).slice(-8).reverse();
    const summary = element("div", undefined, "graph-summary");
    [["Workflow", workflowId], ["Active revision", active.revision ?? "None"], ["Definitions", definitions.length], ["State", graph.empty ? "No definition yet" : "Ready"]].forEach(([label, value]) => {
      const card = element("div", undefined, "structured-card");
      card.append(element("span", label), element("strong", value));
      summary.append(card);
    });
    graphOutput.append(summary);
    if (!definitions.length) {
      graphOutput.append(element("div", text(graph.message, "No graph definition is available for this workflow."), "empty"));
      return;
    }
    const latest = map(definitions[0]);
    const actionDefinition = definitionDocument(latest);
    definitions.forEach((definition) => {
      const current = map(definition);
      const revision = current.revision ?? "?";
      const card = element("section", undefined, "graph-definition");
      const heading = element("div", undefined, "panel-heading");
      heading.append(element("h3", `Revision ${revision}${current.active ? " · active" : ""}`));
      heading.append(element("span", current.review ? "Reviewed" : "Review pending", `tag ${current.review ? "success" : "warn"}`));
      card.append(heading);
      const meta = element("div", undefined, "graph-meta");
      meta.append(element("span", `Hash: ${text(current.definition_hash, "not recorded")}`));
      const safety = map(map(current.safety_evidence).evidence?.safety || map(current.safety_evidence).safety);
      meta.append(element("span", `Safety: ${safety.passed === true ? "passing" : safety.passed === false ? "blocking" : "unavailable"}`));
      card.append(meta);
      const nodes = list(current.nodes);
      const nodeGrid = element("div", undefined, "graph-nodes");
      nodes.forEach((node) => {
        const value = map(node);
        const nodeCard = element("div", undefined, "graph-node");
        nodeCard.append(element("strong", text(value.node_id, "Unnamed node")), element("span", text(value.kind, "step")));
        nodeGrid.append(nodeCard);
      });
      if (nodes.length) card.append(nodeGrid);
      const edges = list(current.edges);
      const edgeList = element("ul", undefined, "structured-list");
      edges.forEach((edge) => {
        const value = map(edge);
        edgeList.append(element("li", `${text(value.source, "?")} → ${text(value.target, "?")} · ${text(value.condition, "always")}`));
      });
      if (edges.length) {
        card.append(element("h3", "Transitions"), edgeList);
      }
      const actions = element("div", undefined, "settings-actions");
      if (current === latest) {
        const base = { workflow_id: workflowId, candidate: actionDefinition, fixtures: definitionFixtures(actionDefinition) };
        actions.append(actionButton("Evaluate draft", { testid: "graph-evaluate", payload: { action: "graph_evaluate", ...base } }, "secondary"));
        actions.append(actionButton("Record review", { testid: "graph-review", payload: { action: "graph_review", ...base } }, "secondary"));
        actions.append(actionButton("Activate reviewed draft", { testid: "graph-activate", payload: { action: "graph_activate", ...base } }, "primary"));
      }
      if (active.revision && Number(revision) < Number(active.revision) && current.review) {
        actions.append(actionButton(`Rollback to revision ${revision}`, { testid: "graph-rollback", payload: { action: "graph_rollback", workflow_id: workflowId, revision } }, "secondary"));
      }
      if (actions.children.length) card.append(actions);
      graphOutput.append(card);
    });
  }

  function renderWorkflowSelectors(state) {
    const graph = map(state.graph);
    const selected = text(graph.workflow_id, settingsWorkflowInput?.value || "");
    const ids = list(state.workflow_ids).filter((value) => typeof value === "string" && value.trim());
    if (selected && !ids.includes(selected)) ids.unshift(selected);
    [workflowSelector, settingsWorkflowInput].filter(Boolean).forEach((select) => {
      select.replaceChildren();
      if (!ids.length) {
        const option = element("option", "No workflows available");
        option.value = "";
        option.disabled = true;
        option.selected = true;
        select.append(option);
        return;
      }
      const empty = element("option", "No workflow selected");
      empty.value = "";
      select.append(empty);
      ids.forEach((workflowId) => {
        const option = element("option", workflowId);
        option.value = workflowId;
        option.selected = workflowId === selected;
        select.append(option);
      });
      if (!selected) empty.selected = true;
    });
  }

  function renderProjectsPage(state) {
    if (!projectsOutput) return;
    projectsOutput.replaceChildren();
    const repositories = list(state.repositories);
    const summary = element("div", undefined, "graph-summary");
    [["Project ID", text(state.project_id, "Unavailable")], ["Display name", text(state.name, "Unnamed project")], ["Repositories", repositories.length], ["Work items", allItems(state).length]].forEach(([label, value]) => {
      const card = element("div", undefined, "structured-card");
      card.append(element("span", label), element("strong", value));
      summary.append(card);
    });
    projectsOutput.append(summary);
    if (!repositories.length) {
      projectsOutput.append(element("div", "The service has no linked repositories in this project.", "empty"));
      return;
    }
    const listNode = element("ul", undefined, "structured-list");
    repositories.forEach((repository) => {
      const writer = map(repository.writer);
      const itemCount = list(repository.pbis).length;
      const row = element("li");
      row.append(element("strong", text(repository.name, "Unnamed repository")), element("span", `${itemCount} work item${itemCount === 1 ? "" : "s"} · writer ${text(writer.status, "idle")}`));
      listNode.append(row);
    });
    projectsOutput.append(listNode);
  }

  function renderActivity(state) {
    activityOutput.replaceChildren();
    const actions = list(state.actions).slice(-8).reverse();
    if (!actions.length) {
      activityOutput.append(element("div", "No operator actions yet.", "empty"));
      return;
    }
    const listNode = element("ul", undefined, "activity-list");
    actions.forEach((action) => {
      const row = element("li");
      const copy = element("span");
      copy.append(element("span", `${ACTION_LABELS[action.kind] || text(action.kind, "Action")} · ${text(action.status, "unknown")}${action.repository ? ` · ${action.repository}` : ""}${action.pbi_number ? `#${action.pbi_number}` : ""}`, "activity-main"), element("span", activityDetail(action), "activity-detail"));
      row.append(element("time", text(action.created_at, "Recent")), copy);
      listNode.append(row);
    });
    activityOutput.append(listNode);
  }

  function renderScheduler(state) {
    schedulerCard.replaceChildren();
    const scheduler = map(state.scheduler);
    const enabled = scheduler.enabled === true;
    const configured = Object.keys(scheduler).length > 0;
    const title = element("div", undefined, "scheduler-title");
    title.append(element("span", "", "status-dot"), element("span", !configured ? "Scheduler unavailable" : enabled ? "Scheduler running" : "Scheduler off"));
    const active = text(scheduler.active_workers, "0");
    const capacity = text(scheduler.max_concurrency, "?");
    schedulerCard.append(title, element("p", !configured ? "Configure a workflow-backed agent worker to enable autonomous polling." : enabled ? `Polling allowlisted projects. Last poll ${text(scheduler.last_poll_at, "not observed")}.` : "Autonomous polling is off. Enable it in Settings when you want the server to pick up work."));
    const bars = element("div", undefined, "capacity");
    const total = Number(capacity) || 1;
    const used = Math.min(Number(active) || 0, total);
    for (let index = 0; index < total; index += 1) bars.append(element("span", "", index < used ? "used" : ""));
    schedulerCard.append(bars, element("div", `${active} of ${capacity} workers leased`, "capacity-label"));
  }

  function render(state) {
    currentState = state || {};
    if (typeof currentState.archived === "boolean") {
      currentFilter = currentState.archived ? "archived" : currentFilter === "archived" ? "all" : currentFilter;
    }
    const items = allItems(currentState);
    const boardItems = currentProjectFilter === "all"
      ? items
      : items.filter((item) => item.project === currentProjectFilter);
    const queueCount = document.querySelector("#nav-queue-count");
    const agentCount = document.querySelector("#nav-agent-count");
    if (queueCount) queueCount.textContent = String(items.length);
    if (agentCount) agentCount.textContent = String(items.filter(isAgentActive).length);
    renderProjectSwitcher(currentState);
    renderWorkflowSelectors(currentState);
    refreshAge.textContent = `refreshed ${text(currentState.updated_at, "just now")}`;
    renderMetrics(boardItems);
    renderAttention(boardItems);
    renderQueue(boardItems);
    renderAgents(boardItems);
    renderActivity(currentState);
    renderDeliveries(currentState, boardItems);
    renderScheduler(currentState);
    renderGraphPage(currentState);
    renderProjectsPage(currentState);
  }

  function setPage(page) {
    currentPage = page || "mission";
    const main = document.querySelector("#mission");
    main?.classList.remove(...["mission", "queue", "agents", "deliveries", "workflow-graphs", "projects", "evidence", "settings"].map((value) => `page-${value}`));
    main?.classList.add(`page-${currentPage}`);
    document.querySelectorAll("[data-page-content]").forEach((node) => {
      const pages = String(node.dataset.pageContent || "").split(/\s+/);
      const visible = pages.includes(currentPage);
      node.classList.toggle("page-content-hidden", !visible);
      if (node.classList.contains("page-panel")) node.hidden = !visible;
    });
  }

  return { render, closeInspector, setPage };
}
