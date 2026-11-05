import {
  folderName,
  formatAgo,
  formatClock,
  formatDuration,
  formatTokens,
  splitIssueRefs,
  extraArgs,
  stageText,
} from "./format.js";

const REFRESH_MS = 3000;
const state = {
  sessions: [],
  indexing: true,
  detail: null,
  agent: null,
  sessionId: null,
  agentId: null,
  error: null,
};

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "onclick") node.addEventListener("click", value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === "") continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function link(href, text) {
  return el("a", { href, target: "_blank", rel: "noreferrer" }, text);
}

function linkedText(text, repositoryUrl) {
  return splitIssueRefs(text, repositoryUrl).map((piece) =>
    piece.href ? link(piece.href, piece.text) : piece.text,
  );
}

function dot(status) {
  return el("span", { class: `dot ${status ?? "idle"}`, title: status ?? "" });
}

function readHash() {
  const params = new URLSearchParams(location.hash.slice(1));
  state.sessionId = params.get("session");
  state.agentId = params.get("agent");
}

function writeHash() {
  const params = new URLSearchParams();
  if (state.sessionId) params.set("session", state.sessionId);
  if (state.agentId) params.set("agent", state.agentId);
  history.replaceState(null, "", `#${params}`);
}

async function getJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`${url} answered ${response.status}`);
  return response.json();
}

async function refresh() {
  try {
    const listing = await getJson("/api/sessions");
    state.sessions = listing.sessions;
    state.indexing = listing.indexing;
    if (!state.sessionId && state.sessions.length) state.sessionId = state.sessions[0].id;
    const base = state.sessionId ? `/api/sessions/${encodeURIComponent(state.sessionId)}` : null;
    state.detail = base ? await getJson(base).catch(() => null) : null;
    // A stale agent id in the hash falls back to the session's root agent.
    const agents = state.detail?.agents ?? [];
    const agentId = agents.some((agent) => agent.id === state.agentId)
      ? state.agentId
      : agents[0]?.id;
    state.agent =
      base && agentId
        ? await getJson(`${base}/agents/${encodeURIComponent(agentId)}`).catch(() => null)
        : null;
    state.error = null;
  } catch (error) {
    state.error = error.message;
  }
  render();
}

function select(sessionId, agentId = null) {
  state.sessionId = sessionId;
  state.agentId = agentId;
  writeHash();
  refresh();
}

// Panes that scroll on their own; a re-render must not move them.
const SCROLLERS = [".sessions", ".detail", ".agent-tree", ".agent-detail"];

function render() {
  const scrolled = SCROLLERS.map((selector) => document.querySelector(selector)?.scrollTop ?? 0);
  draw();
  SCROLLERS.forEach((selector, i) => {
    const node = document.querySelector(selector);
    if (node) node.scrollTop = scrolled[i];
  });
}

function draw() {
  const now = Date.now();
  document.getElementById("refresh-state").textContent = state.error
    ? `Refresh failed: ${state.error}`
    : `${state.indexing ? "Reading transcripts... " : ""}Updated ${formatClock(new Date(now).toISOString())}`;
  renderSessions(now);
  renderDetail(now);
}

function renderSessions(now) {
  const showIdle = document.getElementById("show-idle").checked;
  const list = document.getElementById("session-list");
  const visible = state.sessions.filter(
    (session) => showIdle || session.status === "working" || session.id === state.sessionId,
  );
  document.getElementById("session-empty").hidden = visible.length > 0;
  list.replaceChildren(
    ...visible.map((session) => {
      const current = session.current;
      return el(
        "li",
        {
          class: `session-card${session.id === state.sessionId ? " selected" : ""}`,
          onclick: () => select(session.id),
        },
        el(
          "div",
          { class: "card-head" },
          dot(session.status),
          el("strong", {}, session.title),
          session.workflow ? el("span", { class: "badge" }, session.workflow) : null,
        ),
        el(
          "div",
          { class: "muted small" },
          folderName(session.cwd),
          session.branch ? ` on ${session.branch}` : "",
        ),
        session.stage ? el("div", { class: "stage" }, stageText(session.stage)) : null,
        current
          ? el("div", { class: "small current" }, `${current.name}: ${current.detail}`)
          : null,
        el(
          "div",
          { class: "muted small" },
          `${session.status} - ${formatAgo(session.last_activity, now)}`,
          session.counts.working_agents > 1
            ? ` - ${session.counts.working_agents} agents working`
            : "",
          session.counts.problems ? ` - ${session.counts.problems} problems` : "",
        ),
      );
    }),
  );
}

function renderDetail(now) {
  const container = document.getElementById("detail");
  const detail = state.detail;
  if (!detail) {
    container.replaceChildren(el("p", { class: "muted" }, "Pick a session to see its agents and steps."));
    return;
  }
  const repoUrl = detail.repository?.url ?? null;
  const agents = detail.agents;
  const selected = agents.find((agent) => agent.id === state.agentId) ?? agents[0];
  const full = state.agent && state.agent.id === selected.id ? state.agent : null;
  container.replaceChildren(
    el(
      "div",
      { class: "detail-head" },
      el("h2", {}, dot(detail.status), " ", detail.title),
      el(
        "div",
        { class: "links" },
        detail.repository ? link(detail.repository.url, detail.repository.name) : folderName(detail.cwd),
        ...detail.projects.map((project) => link(project.url, `Project: ${project.title}`)),
        ...detail.pull_requests.map((pr) => link(pr.url, `PR #${pr.number}`)),
      ),
      el(
        "div",
        { class: "muted small" },
        `Started ${formatClock(detail.started_at)} - ran ${formatDuration(detail.duration_s)}`,
        ` - ${formatTokens(detail.tokens.input)} in / ${formatTokens(detail.tokens.output)} out`,
        detail.counts.unreadable_records
          ? ` - ${detail.counts.unreadable_records} records could not be read`
          : "",
      ),
      detail.stage
        ? el(
            "div",
            { class: "stage" },
            "goal-sdlc stage: ",
            detail.stage.issue_url
              ? link(detail.stage.issue_url, stageText(detail.stage))
              : stageText(detail.stage),
            ` (${detail.stage.status})`,
          )
        : null,
    ),
    el(
      "div",
      { class: "detail-body" },
      el("div", { class: "agent-tree" }, el("h3", {}, `Agents (${agents.length})`), ...renderTree(agents, selected, now)),
      full
        ? renderAgent(full, repoUrl, now)
        : el("div", { class: "agent-detail muted" }, "Loading agent..."),
    ),
  );
}

function renderTree(agents, selected, now) {
  return agents.map((agent) =>
    el(
      "button",
      {
        class: `agent-row${agent.id === selected.id ? " selected" : ""}`,
        style: `padding-left: ${0.5 + agent.depth * 1.1}rem`,
        onclick: () => select(state.sessionId, agent.id),
        type: "button",
      },
      dot(agent.status),
      el("span", { class: "agent-title" }, agent.label ? stageText(agent.label) : agent.title),
      el(
        "span",
        { class: "muted small" },
        agent.status === "working"
          ? `working ${formatDuration(agent.duration_s ?? 0)}`
          : `${agent.status} - ${formatDuration(agent.duration_s)}`,
        agent.problem_count ? ` - ${agent.problem_count} problems` : "",
        agent.status !== "working" ? ` - ${formatAgo(agent.last_activity, now)}` : "",
      ),
    ),
  );
}

function renderAgent(agent, repoUrl, now) {
  const steps = [...agent.steps].reverse();
  const problems = [...agent.problems].reverse();
  return el(
    "div",
    { class: "agent-detail" },
    el("h3", {}, dot(agent.status), " ", agent.title),
    el(
      "div",
      { class: "muted small" },
      [agent.agent_type, agent.model].filter(Boolean).join(" - "),
      ` - ${formatTokens(agent.tokens.input)} in / ${formatTokens(agent.tokens.output)} out`,
      ` - last activity ${formatAgo(agent.last_activity, now)}`,
    ),
    agent.label?.issue_url
      ? el(
          "div",
          { class: "stage" },
          link(agent.label.issue_url, stageText(agent.label)),
          ` ${extraArgs(agent.label)}`,
        )
      : null,
    agent.current
      ? el(
          "div",
          { class: "now" },
          el("strong", {}, agent.current.status === "running" ? "Doing now: " : "Last step: "),
          `${agent.current.name} - `,
          ...linkedText(agent.current.detail, repoUrl),
          agent.current.duration_s !== null ? ` (${formatDuration(agent.current.duration_s)})` : "",
        )
      : null,
    agent.last_message
      ? el("blockquote", { class: "last-message" }, ...linkedText(agent.last_message, repoUrl))
      : null,
    problems.length
      ? el(
          "div",
          { class: "problems" },
          el("h4", {}, `Problems (${problems.length})`),
          el(
            "ul",
            {},
            ...problems.map((problem) =>
              el("li", {}, el("span", { class: "muted small" }, `${formatClock(problem.at)} ${problem.kind} `), problem.message),
            ),
          ),
        )
      : null,
    el(
      "h4",
      {},
      `Steps (${agent.step_count}${agent.step_count > agent.steps.length ? `, newest ${agent.steps.length} shown` : ""})`,
    ),
    el(
      "table",
      { class: "steps" },
      el("thead", {}, el("tr", {}, el("th", {}, "Time"), el("th", {}, "Took"), el("th", {}, "Step"), el("th", {}, "Detail"))),
      el(
        "tbody",
        {},
        ...steps.map((step) =>
          el(
            "tr",
            { class: `step ${step.status} ${step.kind}` },
            el("td", { class: "muted" }, formatClock(step.started_at)),
            el("td", {}, step.kind === "prompt" ? "" : formatDuration(step.duration_s)),
            el(
              "td",
              {},
              step.child_agent_id
                ? el("a", { href: "#", onclick: (event) => { event.preventDefault(); select(state.sessionId, step.child_agent_id); } }, step.name)
                : step.name,
            ),
            el("td", {}, ...linkedText(step.detail, repoUrl), step.error ? el("div", { class: "error" }, step.error) : null),
          ),
        ),
      ),
    ),
  );
}

readHash();
document.getElementById("show-idle").addEventListener("change", render);
window.addEventListener("hashchange", () => {
  readHash();
  refresh();
});
refresh();
// Skip a timed refresh while text is selected, so it can be copied.
setInterval(() => {
  if (!getSelection()?.toString()) refresh();
}, REFRESH_MS);
