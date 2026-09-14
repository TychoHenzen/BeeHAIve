export function createDetailRenderers(dom) {
  const { element, renderListSection, checkStatusClass } = dom;
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

  function renderOperatorQuestion(question) {
    const section = element("section");
    section.append(element("h3", `Operator question: ${question.status || "unknown"}`));
    section.append(element("div", question.question || "Question text unavailable", "status pending"));
    section.append(
      element(
        "div",
        `Question ${question.question_id || "unknown"} · revision ${question.revision ?? "?"}`,
        "mono",
      ),
    );
    if (question.kind) section.append(element("div", `Reason: ${question.kind}`, "muted"));
    if (question.answer) section.append(element("div", `Answer: ${question.answer}`, "muted"));
    section.append(
      element(
        "div",
        `Notification: ${question.notification_status || "unknown"} · ${question.notification_attempts || 0} attempts`,
        question.notification_status === "delivered" ? "status success" : "status pending",
      ),
    );
    if (question.notification_last_error) {
      section.append(element("div", question.notification_last_error, "status failure"));
    }
    if (question.evidence && typeof question.evidence === "object") {
      section.append(element("div", `Evidence: ${JSON.stringify(question.evidence)}`, "muted"));
    }
    return section;
  }

  function renderProgress(progress) {
    const list = element("ol", undefined, "progress");
    progress.forEach((stage) => list.append(element("li", stage.label, stage.status)));
    return list;
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

  function renderSubtask(item) {
    const label = `${item.id || "subtask"}: ${item.title || ""}`;
    const status = item.readiness || "unknown";
    const issue = item.issue_state
      ? `Issue ${item.issue_state}${item.state_reason ? ` (${item.state_reason})` : ""}`
      : "Issue state unavailable";
    const project = item.project_status
      ? `Project status ${item.project_status}`
      : "Project status unavailable";
    const blockers = item.dependency_read_complete
      ? `Blocked by ${(item.blocked_by || []).map((blocker) => `#${blocker.number} ${blocker.state}${blocker.state_reason ? ` (${blocker.state_reason})` : ""}`).join(", ") || "none"}`
      : `Blocked-by facts unavailable (${item.dependency_read_error || "read incomplete"})`;
    const reasons = (item.readiness_reasons || []).join(", ");
    const observed = item.observed_at ? ` Observed: ${item.observed_at}.` : "";
    return `${label}. Readiness: ${status}. ${issue}. ${project}. ${blockers}.${reasons ? ` Reasons: ${reasons}.` : ""}${observed}`;
  }

  function renderDependencyReadiness(readiness) {
    const section = element("section");
    section.append(element("h3", "Dependency readiness"));
    if (!readiness || typeof readiness !== "object") {
      section.append(element("p", "Unknown. Readiness evidence is unavailable.", "muted"));
      return section;
    }
    const status = readiness.status || "unknown";
    const counts = readiness.counts || {};
    const summary = ["ready", "incomplete", "blocked", "rejected", "completed", "unknown"]
      .map((outcome) => `${outcome}: ${counts[outcome] || 0}`)
      .join("; ");
    section.append(element("p", `Parent readiness: ${status}. ${summary}`));
    const reasons = Array.isArray(readiness.reasons) ? readiness.reasons : [];
    if (reasons.length > 0) {
      section.append(element("p", `Reasons: ${reasons.join(", ")}`, "muted"));
    }
    if (readiness.observed_at) {
      section.append(element("p", `Observed: ${readiness.observed_at}`, "muted"));
    }
    return section;
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
  return { renderTaskContract, renderTaskResult, renderOperatorQuestion, renderProgress, renderChecks, renderSubtask, renderDependencyReadiness, renderReviewers, renderEscalation, renderActivity };
}
