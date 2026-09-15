export function createPbiRenderer(dom, details, runAction) {
  const { element, renderEvidenceLink, renderListSection, pullRequestLabel } = dom;
  const { renderTaskContract, renderTaskResult, renderOperatorQuestion, renderProgress, renderAgentSession, renderSubtask, renderDependencyReadiness, renderChecks, renderReviewers, renderEscalation, renderActivity } = details;
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
    if (pbi.issue_state) {
      const reason = pbi.state_reason ? ` (${pbi.state_reason})` : "";
      card.append(element("div", `Issue ${pbi.issue_state}${reason}`, "muted"));
    }
    if (pbi.branch) card.append(element("div", `Branch: ${pbi.branch}`, "muted"));
    if (pbi.pull_request_url) card.append(renderEvidenceLink("Pull request", pbi.pull_request_url));
    card.append(renderProgress(pbi.stage_progress || []));
    const details = element("div", undefined, "details");
    const agentSession = renderAgentSession(pbi.agent_session);
    if (agentSession) details.append(agentSession);
    if (pbi.task_contract) details.append(renderTaskContract(pbi.task_contract));
    const operatorQuestions = pbi.operator_questions || [];
    const hasPendingOperatorQuestion = operatorQuestions.some(
      (question) => question.status === "pending",
    );
    if (operatorQuestions.length > 0) {
      operatorQuestions.forEach((question) => {
        const section = renderOperatorQuestion(question);
        if (question.status === "pending" && pbi.run_id) {
          const answer = element("button", "Answer operator question", "secondary");
          answer.type = "button";
          answer.addEventListener("click", () => {
            const value = window.prompt(question.question || "Answer this question:");
            if (value && value.trim()) {
              runAction({
                action: "answer_question",
                repository,
                pbi_number: pbi.number,
                run_id: pbi.run_id,
                question_id: question.question_id,
                revision: question.revision,
                answer: value.trim(),
              });
            }
          });
          section.append(answer);
        }
        details.append(section);
      });
    }
    if (pbi.task_result && !hasPendingOperatorQuestion) {
      details.append(renderTaskResult(pbi.task_result));
    }
    details.append(renderListSection("Subtasks", pbi.subtasks, renderSubtask));
    if (pbi.dependency_readiness || (pbi.subtasks || []).length > 0) {
      details.append(renderDependencyReadiness(pbi.dependency_readiness));
    }
    if (pbi.planning_status) card.append(element("div", `Project status: ${pbi.planning_status}`, "muted"));
    details.append(renderListSection("Pull requests", pbi.pull_requests, (item) => pullRequestLabel(item)));
    if (pbi.checks && typeof pbi.checks === "object") details.append(renderChecks(pbi.checks));
    details.append(renderListSection("Readers", pbi.readers, (item) => `${item.pull_request ? `PR #${item.pull_request} ` : ""}${item.id || item.name || "reader"}: ${item.status || "pending"}`));
    details.append(renderReviewers(pbi.reviewers || {}));
    details.append(renderEscalation(pbi.escalation, pbi.escalation_log));
    details.append(renderActivity(pbi.activity || []));
    card.append(details);
    if (pbi.delivery) {
      const delivery = element("section");
      delivery.append(element("h3", `Git delivery: ${pbi.delivery.status}`));
      if (pbi.delivery.commit_sha) {
        delivery.append(element("div", `Commit: ${pbi.delivery.commit_sha}`, "mono"));
      }
      if (pbi.delivery.evidence) {
        delivery.append(element("div", pbi.delivery.evidence, "muted"));
      }
      card.append(delivery);
      if (pbi.delivery.retry_available && pbi.run_id) {
        const retry = element("button", "Retry commit and push", "secondary");
        retry.type = "button";
        retry.addEventListener("click", () => runAction({
          action: "commit_push",
          repository,
          pbi_number: pbi.number,
          run_id: pbi.run_id,
        }));
        const controls = element("div", undefined, "actions");
        controls.append(retry);
        card.append(controls);
      }
    }
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
  return renderPbi;
}
