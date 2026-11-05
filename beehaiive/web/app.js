import {
  backgroundText,
  folderName,
  formatAgo,
  formatClock,
  formatDuration,
  formatTokens,
  plural,
  shortStage,
  splitIssueRefs,
  toolSummary,
} from "./format.js";

const REFRESH_MS = 3000;
const state = {
  sessions: [],
  indexing: true,
  detail: null,
  agent: null,
  sessionId: null,
  agentId: null,
  stepsOpen: false,
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
  return el("a", { href, target: "_blank", rel: "noreferrer", onclick: (e) => e.stopPropagation() }, text);
}

function issueLink(issue, url) {
  if (!issue) return null;
  return url ? link(url, `#${issue}`) : `#${issue}`;
}

function linkedText(text, repositoryUrl) {
  return splitIssueRefs(text, repositoryUrl).map((piece) =>
    piece.href ? link(piece.href, piece.text) : piece.text,
  );
}

function dot(status) {
  return el("span", { class: `dot ${status ?? "idle"}`, title: status ?? "" });
}

function section(title, ...children) {
  return el("section", { class: "block" }, el("h3", {}, title), ...children);
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

// Only the newest refresh may draw, so a slow answer for the previously
// selected session cannot replace the one just picked.
let latestRefresh = 0;

async function refresh() {
  const ticket = ++latestRefresh;
  const next = await load();
  if (ticket !== latestRefresh) return;
  Object.assign(state, next);
  render();
}

async function load() {
  const next = { error: null };
  try {
    const listing = await getJson("/api/sessions");
    next.sessions = listing.sessions;
    next.indexing = listing.indexing;
    if (!state.sessionId && listing.sessions.length) state.sessionId = listing.sessions[0].id;
    const base = state.sessionId ? `/api/sessions/${encodeURIComponent(state.sessionId)}` : null;
    const agentId = state.agentId;
    next.detail = base ? await getJson(base).catch(() => null) : null;
    // A stale agent id in the hash just closes the agent panel.
    next.agent =
      base && agentId
        ? await getJson(`${base}/agents/${encodeURIComponent(agentId)}`).catch(() => null)
        : null;
  } catch (error) {
    next.error = error.message;
  }
  return next;
}

function select(sessionId, agentId = null) {
  if (sessionId !== state.sessionId || agentId !== state.agentId) state.stepsOpen = false;
  state.sessionId = sessionId;
  state.agentId = agentId;
  writeHash();
  refresh();
}

function openAgent(agentId) {
  return (event) => {
    event.stopPropagation();
    select(state.sessionId, agentId);
  };
}

// Panes that scroll on their own; a re-render must not move them.
const SCROLLERS = [".sessions", ".overview", ".agent-panel"];

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

// Left column: what is running now, then everything else in brief.

function renderSessions(now) {
  const running = state.sessions.filter((s) => s.status === "working");
  const recent = state.sessions.filter((s) => s.status !== "working");
  document.getElementById("running-list").replaceChildren(...running.map((s) => runningCard(s)));
  document.getElementById("running-empty").hidden = running.length > 0;
  document.getElementById("recent-list").replaceChildren(...recent.map((s) => recentRow(s, now)));
}

function attentionBadge(count) {
  return count ? el("span", { class: "badge attention", title: "Needs attention" }, String(count)) : null;
}

function nowLines(now) {
  if (!now) return [];
  if (now.stages.length) {
    return now.stages.map((stage) =>
      el(
        "div",
        { class: "now-line" },
        el("strong", {}, shortStage(stage.stage)),
        stage.issue ? " " : "",
        issueLink(stage.issue, stage.issue_url),
        el("span", { class: "muted" }, ` running ${formatDuration(stage.duration_s)}`),
      ),
    );
  }
  return [
    el(
      "div",
      { class: "now-line" },
      el("span", { class: "request" }, now.request || "Working"),
      el("span", { class: "muted" }, ` - ${formatDuration(now.running_s)}`),
    ),
  ];
}

function liveLines(live, more) {
  const lines = live.map((agent) =>
    el(
      "div",
      { class: "live-line", style: `padding-left: ${(agent.depth - 1) * 0.9}rem` },
      dot("working"),
      el("span", { class: "live-title" }, agent.title),
      el("span", { class: "muted" }, ` ${formatDuration(agent.running_s)}`),
      agent.doing ? el("div", { class: "doing muted small" }, agent.doing) : null,
    ),
  );
  if (more) lines.push(el("div", { class: "muted small" }, `and ${more} more`));
  return lines;
}

function runningCard(session) {
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
      attentionBadge(session.attention_count),
    ),
    el("div", { class: "muted small" }, folderName(session.cwd), session.branch ? ` on ${session.branch}` : ""),
    ...nowLines(session.now),
    ...liveLines(session.live, session.live_more),
  );
}

function recentRow(session, now) {
  return el(
    "li",
    {
      class: `recent-row${session.id === state.sessionId ? " selected" : ""}`,
      onclick: () => select(session.id),
    },
    dot(session.status),
    el("span", { class: "recent-title" }, session.title),
    session.workflow ? el("span", { class: "badge" }, "sdlc") : null,
    attentionBadge(session.attention_count),
    el("span", { class: "muted small" }, formatAgo(session.last_activity, now)),
  );
}

// Right column: one session.

function renderDetail(now) {
  const container = document.getElementById("detail");
  const detail = state.detail;
  if (!detail) {
    container.replaceChildren(el("p", { class: "muted" }, "Pick a session on the left."));
    return;
  }
  const repoUrl = detail.repository?.url ?? null;
  const overview = el(
    "div",
    { class: "overview" },
    sessionHead(detail),
    detail.status === "working" ? nowBlock(detail) : null,
    attentionBlock(detail.attention, now),
    detail.workflow === "goal-sdlc" ? issuesBlock(detail) : turnsBlock(detail.turns, repoUrl),
  );
  const panel = state.agent ? agentPanel(state.agent, repoUrl, now) : null;
  container.replaceChildren(el("div", { class: `detail-body${panel ? " with-agent" : ""}` }, overview, panel));
}

function sessionHead(detail) {
  const background = backgroundText(detail.background);
  return el(
    "div",
    { class: "detail-head" },
    el("h2", {}, dot(detail.status), " ", detail.title),
    el(
      "div",
      { class: "links" },
      detail.repository ? link(detail.repository.url, detail.repository.name) : folderName(detail.cwd),
      detail.branch ? el("span", { class: "muted" }, detail.branch) : null,
      ...detail.projects.map((project) => link(project.url, `Project: ${project.title}`)),
      ...detail.pull_requests.map((pr) => link(pr.url, `PR #${pr.number}`)),
    ),
    el(
      "div",
      { class: "muted small" },
      `Started ${formatClock(detail.started_at)}, ran ${formatDuration(detail.duration_s)}, `,
      `${formatTokens(detail.tokens.input)} tokens in and ${formatTokens(detail.tokens.output)} out.`,
      background ? ` Handled along the way: ${background}.` : "",
    ),
  );
}

function nowBlock(detail) {
  return section(
    "Running now",
    ...nowLines(detail.now),
    detail.now?.doing ? el("div", { class: "doing muted small" }, `Main thread: ${detail.now.doing}`) : null,
    ...detail.live.map((agent) =>
      el(
        "button",
        { type: "button", class: "live-line clickable", style: `padding-left: ${(agent.depth - 1) * 0.9 + 0.4}rem`, onclick: openAgent(agent.agent_id) },
        dot("working"),
        el("span", { class: "live-title" }, agent.title),
        el("span", { class: "muted" }, ` ${formatDuration(agent.running_s)}`),
        agent.doing ? el("div", { class: "doing muted small" }, agent.doing) : null,
      ),
    ),
    detail.live_more ? el("div", { class: "muted small" }, `and ${detail.live_more} more`) : null,
  );
}

const ATTENTION_TEXT = { stalled: "went quiet", stopped: "stopped", failed: "failed" };

function attentionBlock(items, now) {
  if (!items.length) return section("Needs attention", el("p", { class: "muted" }, "Nothing needs you."));
  return section(
    `Needs attention (${items.length})`,
    el(
      "ul",
      { class: "attention-list" },
      ...items.map((item) =>
        el(
          "li",
          { class: `attention-item ${item.kind}`, onclick: openAgent(item.agent_id) },
          el(
            "div",
            {},
            el("strong", {}, shortStage(item.stage) || item.title),
            item.issue ? " " : "",
            issueLink(item.issue, item.issue_url),
            ` ${ATTENTION_TEXT[item.kind] ?? item.kind}`,
            el("span", { class: "muted small" }, ` ${formatAgo(item.at, now)}`),
          ),
          item.note ? el("div", { class: "note" }, item.note) : null,
        ),
      ),
    ),
  );
}

function stageChip(chip) {
  return el(
    "button",
    {
      type: "button",
      class: `chip ${chip.outcome}${chip.agent_id === state.agentId ? " selected" : ""}`,
      title: chip.note || chip.outcome,
      onclick: openAgent(chip.agent_id),
    },
    el("span", { class: "chip-name" }, shortStage(chip.stage)),
    el("span", { class: "chip-time" }, formatDuration(chip.duration_s)),
  );
}

function issuesBlock(detail) {
  const upkeep = detail.upkeep;
  return section(
    `Issues in this run (${detail.issues.length})`,
    detail.issues.length ? null : el("p", { class: "muted" }, "No stage has named an issue yet."),
    el(
      "div",
      { class: "issue-rows" },
      ...detail.issues.map((row) =>
        el(
          "div",
          { class: `issue-row ${row.outcome}` },
          el(
            "div",
            { class: "issue-head" },
            el("strong", {}, issueLink(row.issue, row.issue_url)),
            el("span", { class: "muted small" }, formatDuration(row.duration_s)),
          ),
          el("div", { class: "chips" }, ...row.stages.map(stageChip)),
        ),
      ),
    ),
    upkeep?.runs
      ? el(
          "p",
          { class: "muted small" },
          `Queue upkeep with no issue: ${plural(upkeep.runs, "run")}, ${formatDuration(upkeep.duration_s)} in total`,
          upkeep.running ? `, ${upkeep.running} running now.` : ".",
        )
      : null,
  );
}

function turnsBlock(turns, repoUrl) {
  return section(
    `Turns (${turns.length})`,
    turns.length ? null : el("p", { class: "muted" }, "No request has been answered yet."),
    ...turns.map((turn) =>
      el(
        "article",
        { class: `turn${turn.running ? " running" : ""}` },
        el(
          "div",
          { class: "turn-head" },
          el("span", { class: "muted small" }, formatClock(turn.started_at)),
          el("span", { class: "small" }, turn.running ? `working for ${formatDuration(turn.duration_s)}` : `worked ${formatDuration(turn.duration_s)}`),
        ),
        el("div", { class: "request" }, ...linkedText(turn.request || "(work before the first request)", repoUrl)),
        turn.reply ? el("div", { class: "reply" }, ...linkedText(turn.reply, repoUrl)) : null,
        el(
          "div",
          { class: "muted small" },
          toolSummary(turn.tool_calls, turn.top_tools),
          turn.errors ? `, ${plural(turn.errors, "tool error")} recovered` : "",
        ),
        turn.subagents.length
          ? el(
              "div",
              { class: "chips" },
              ...turn.subagents.map((child) =>
                el(
                  "button",
                  { type: "button", class: `chip ${child.outcome}`, title: child.note || child.outcome, onclick: openAgent(child.agent_id) },
                  el("span", { class: "chip-name" }, child.title),
                  el("span", { class: "chip-time" }, formatDuration(child.duration_s)),
                ),
              ),
            )
          : null,
      ),
    ),
  );
}

// The selected agent: what it was asked, how it ended, what it started.

const OUTCOME_TEXT = {
  running: "Running",
  done: "Done",
  stopped: "Stopped",
  stalled: "Went quiet",
  failed: "Failed",
};

function agentPanel(agent, repoUrl, now) {
  const steps = [...agent.steps].reverse();
  const problems = Object.entries(agent.problem_counts)
    .map(([kind, count]) => `${kind.replace("_", " ")} ${count}`)
    .join(", ");
  const details = el(
    "details",
    { class: "steps-box", open: state.stepsOpen },
    el("summary", {}, `Every tool call (${agent.step_count}${agent.step_count > agent.steps.length ? `, newest ${agent.steps.length} shown` : ""})`),
    el(
      "table",
      { class: "steps" },
      el(
        "tbody",
        {},
        ...steps.map((step) =>
          el(
            "tr",
            { class: `step ${step.status} ${step.kind}` },
            el("td", { class: "muted" }, formatClock(step.started_at)),
            el("td", {}, step.kind === "prompt" ? "" : formatDuration(step.duration_s)),
            el("td", {}, step.child_agent_id ? el("a", { href: "#", onclick: openAgent(step.child_agent_id) }, step.name) : step.name),
            el("td", {}, ...linkedText(step.detail, repoUrl), step.error ? el("div", { class: "error" }, step.error) : null),
          ),
        ),
      ),
    ),
  );
  details.addEventListener("toggle", () => {
    state.stepsOpen = details.open;
  });
  return el(
    "aside",
    { class: "agent-panel" },
    el(
      "div",
      { class: "panel-head" },
      el("h3", {}, dot(agent.status), " ", agent.stage ? shortStage(agent.stage) : agent.title),
      el("button", { type: "button", class: "close", title: "Close", onclick: () => select(state.sessionId) }, "Close"),
    ),
    agent.stage ? el("div", { class: "muted small" }, agent.title) : null,
    el(
      "div",
      { class: "small" },
      agent.outcome ? el("strong", { class: `outcome ${agent.outcome}` }, OUTCOME_TEXT[agent.outcome] ?? agent.outcome) : null,
      agent.issue ? " - issue " : "",
      issueLink(agent.issue, agent.issue_url),
      ` - ${formatDuration(agent.duration_s)}`,
      ` - ${[agent.agent_type, agent.model].filter(Boolean).join(", ")}`,
      ` - ${formatTokens(agent.tokens.input)} in / ${formatTokens(agent.tokens.output)} out`,
    ),
    agent.doing ? el("div", { class: "now" }, el("strong", {}, agent.status === "working" ? "Doing now: " : "Never finished: "), ...linkedText(agent.doing, repoUrl)) : null,
    agent.stop_reason ? el("div", { class: "stop" }, el("strong", {}, "Stop reason: "), agent.stop_reason) : null,
    agent.result
      ? el("div", { class: "result" }, el("h4", {}, agent.status === "working" ? "Last said" : "Result"), el("blockquote", {}, ...linkedText(agent.result, repoUrl)))
      : null,
    agent.children.length
      ? el(
          "div",
          {},
          el("h4", {}, `Subagents (${agent.children_total})`),
          el(
            "ul",
            { class: "children" },
            ...agent.children
              .slice()
              .reverse()
              .map((child) =>
                el(
                  "li",
                  { class: `child ${child.outcome}`, onclick: openAgent(child.agent_id) },
                  el("span", { class: `chip ${child.outcome}` }, OUTCOME_TEXT[child.outcome] ?? child.outcome),
                  " ",
                  el("strong", {}, child.title),
                  el("span", { class: "muted small" }, ` ${formatDuration(child.duration_s)}`),
                  child.note ? el("div", { class: "note muted small" }, child.note) : null,
                ),
              ),
          ),
        )
      : null,
    el(
      "p",
      { class: "muted small" },
      agent.tool_summary.length ? `Tools: ${agent.tool_summary.slice(0, 8).map(([name, count]) => `${name} ${count}`).join(", ")}.` : "No tool calls.",
      problems ? ` Problems handled: ${problems}.` : "",
      agent.last_activity ? ` Last activity ${formatAgo(agent.last_activity, now)}.` : "",
    ),
    details,
  );
}

readHash();
window.addEventListener("hashchange", () => {
  readHash();
  refresh();
});
refresh();
// Skip a timed refresh while text is selected, so it can be copied.
setInterval(() => {
  if (!getSelection()?.toString()) refresh();
}, REFRESH_MS);
