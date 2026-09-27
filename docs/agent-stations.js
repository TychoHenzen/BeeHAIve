const params = new URLSearchParams(window.location.search);
const projectInput = document.querySelector("#project");
const statusOutput = document.querySelector("#status");
const stationsOutput = document.querySelector("#stations");
const agentsOutput = document.querySelector("#agents");
const issuesOutput = document.querySelector("#issues");
projectInput.value = params.get("project") || "";

function text(value, fallback = "") {
  return typeof value === "string" && value.trim() ? value : fallback;
}

function addText(parent, tag, value, className = "") {
  const node = document.createElement(tag);
  node.textContent = text(value, "—");
  if (className) node.className = className;
  parent.append(node);
  return node;
}

function renderStations(stations) {
  stationsOutput.replaceChildren();
  for (const station of stations || []) {
    const card = document.createElement("article");
    card.className = "station";
    card.dataset.active = String(station.active_agents || 0);
    addText(card, "h2", station.label);
    addText(card, "p", station.description, "muted");
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 240 75");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", `${text(station.label)} station`);
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", "20"); line.setAttribute("x2", "220"); line.setAttribute("y1", "38"); line.setAttribute("y2", "38");
    line.setAttribute("stroke", "#3b4051"); line.setAttribute("stroke-width", "3");
    const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    circle.setAttribute("cx", "120"); circle.setAttribute("cy", "38"); circle.setAttribute("r", "16");
    svg.append(line, circle); card.append(svg);
    addText(card, "p", `${station.active_agents || 0} active agent(s)`);
    stationsOutput.append(card);
  }
}

function renderAgents(agents) {
  agentsOutput.replaceChildren();
  if (!agents?.length) { addText(agentsOutput, "p", "No active agents are persisted.", "muted"); return; }
  for (const agent of agents) {
    const card = document.createElement("article"); card.className = "agent";
    addText(card, "strong", `${text(agent.worker_id, "unassigned")} · ${text(agent.station_label)}`);
    addText(card, "p", `${text(agent.repository)} #${agent.pbi_number}: ${text(agent.title)}`);
    addText(card, "p", `Movement: ${text(agent.movement?.state)} · State: ${text(agent.state)}`);
    const estimate = agent.estimate || {};
    addText(card, "p", estimate.status === "available" ? estimate.basis : estimate.reason);
    const updates = document.createElement("ul"); updates.className = "updates";
    for (const update of agent.updates || []) addText(updates, "li", `${text(update.at)} ${text(update.text)}`);
    card.append(updates); agentsOutput.append(card);
  }
}

function renderIssues(issues) {
  issuesOutput.replaceChildren();
  if (!issues?.length) { addText(issuesOutput, "p", "No durable station issues.", "muted"); return; }
  for (const issue of issues) {
    const card = document.createElement("article"); card.className = "issue"; card.dataset.visible = String(issue.visible !== false);
    addText(card, "strong", `${text(issue.status)} · ${text(issue.station_id)}`);
    addText(card, "p", issue.explanation);
    addText(card, "p", `${text(issue.repository)} #${issue.pbi_number || "?"} · run ${text(issue.run_id, "unknown")}`);
    const attempts = document.createElement("ul"); attempts.className = "updates";
    for (const attempt of issue.attempts || []) addText(attempts, "li", `${text(attempt.action)} by ${text(attempt.actor)}: ${text(attempt.note)}`);
    card.append(attempts);
    if (issue.visible !== false) {
      const actions = document.createElement("div"); actions.className = "issue-actions";
      for (const action of ["address", "dismiss", "resolve"]) {
        const button = document.createElement("button"); button.type = "button"; button.textContent = action;
        button.addEventListener("click", () => actOnIssue(issue.id, action).catch((error) => {
          statusOutput.className = "status error";
          statusOutput.textContent = error.message;
        })); actions.append(button);
      }
      card.append(actions);
    }
    issuesOutput.append(card);
  }
}

async function load() {
  const project = projectInput.value.trim();
  if (!project) { statusOutput.textContent = "Enter a project ID such as owner:123."; return; }
  statusOutput.className = "status"; statusOutput.textContent = "Loading station state...";
  const response = await fetch(`/projects/${encodeURIComponent(project)}/agent-stations`, { cache: "no-store" });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status})`);
  renderStations(payload.stations); renderAgents(payload.agents); renderIssues(payload.issues);
  statusOutput.textContent = `Updated ${new Date(payload.generated_at).toLocaleTimeString()}.`;
  const next = new URL(window.location.href); next.searchParams.set("project", project); window.history.replaceState({}, "", next);
}

async function actOnIssue(issueId, action) {
  const project = projectInput.value.trim();
  const response = await fetch(`/projects/${encodeURIComponent(project)}/agent-stations/issues/${encodeURIComponent(issueId)}/${action}`, {
    method: "POST", headers: { "Content-Type": "application/json", "X-BeeHAIve-Dashboard": "1" }, body: JSON.stringify({ note: "Operator action from agent stations" }),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status})`);
  await load();
}

document.querySelector("#refresh").addEventListener("click", () => void load().catch((error) => { statusOutput.className = "status error"; statusOutput.textContent = error.message; }));
if (projectInput.value) void load().catch((error) => { statusOutput.className = "status error"; statusOutput.textContent = error.message; });
