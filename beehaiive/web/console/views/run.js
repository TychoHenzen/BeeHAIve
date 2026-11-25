// One run: what it is doing, what it needs, its nodes, what its agent nodes
// did, its events and artifacts.
import { activityPanel } from "../activity-panel.js";
import { attentionPanel } from "../attention-panel.js";
import { actionButton, ago, badge, el, notice, replace, runDuration, section } from "../dom.js";
import { agentNodes, eventLine, isLive, pauseKey, runActions, toIso } from "../model.js";

const ACTION_LABELS = { cancel: "Cancel", resume: "Resume", abandon: "Abandon" };
const ACTION_QUESTIONS = {
  cancel: "Cancel this run? Its running nodes are stopped.",
  abandon: "Abandon this run? It is marked cancelled and its owner is stopped.",
  resume: "Resume this run from its completed nodes?",
};
const EVENTS_SHOWN = 150;

function money(value) {
  return typeof value === "number" ? `$${value.toFixed(2)}` : "";
}

function tokens(count) {
  if (typeof count !== "number") return "";
  return count >= 1e6 ? `${(count / 1e6).toFixed(1)}M` : count >= 1e3 ? `${Math.round(count / 1e3)}k` : String(count);
}

function facts(run, projectName) {
  const meta = run.metadata ?? {};
  const rows = [
    ["Project", projectName ?? run.codebase_name ?? run.codebase_id ?? ""],
    ["Started", ago(run.started_at)],
    ["Duration", runDuration(run)],
    ["Cost", money(meta.total_cost_usd)],
    ["Tokens", meta.total_tokens_in ? `${tokens(meta.total_tokens_in)} in, ${tokens(meta.total_tokens_out)} out` : ""],
    ["Worktree", run.working_path ? el("code", {}, run.working_path) : ""],
    ["Run id", el("code", {}, run.id)],
  ];
  return el(
    "dl",
    { class: "facts" },
    rows.filter(([, v]) => v !== "" && v !== null).map(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]),
  );
}

function result(run) {
  const record = run.terminal_record;
  if (!record) return null;
  const value = record.returns?.value;
  return section(
    "Result",
    record.error ? notice(record.error, "error") : null,
    record.first_failed_node ? el("p", {}, "First failed node: ", el("code", {}, record.first_failed_node)) : null,
    value !== undefined ? el("pre", { class: "json" }, JSON.stringify(value, null, 2)) : null,
  );
}

function nodes(run) {
  const list = run.nodes ?? [];
  if (!list.length) return null;
  const shown = list.filter((n) => n.state !== "skipped");
  const skipped = list.length - shown.length;
  return section(
    "Nodes",
    el("ol", { class: "nodes" }, shown.map((n) => el("li", { class: `node state-${n.state}` }, el("span", { class: "dot" }), el("code", {}, n.node_id), el("span", { class: "muted" }, n.state)))),
    skipped ? el("p", { class: "muted small" }, `${skipped} skipped node${skipped === 1 ? "" : "s"} hidden.`) : null,
  );
}

function events(list) {
  if (!list?.length) return null;
  const recent = [...list].sort((a, b) => b.event_order - a.event_order).filter((e) => e.event_type !== "node_skipped");
  return section(
    "Events",
    el(
      "ol",
      { class: "events" },
      recent.slice(0, EVENTS_SHOWN).map((e) => el("li", {}, el("time", { class: "muted small" }, new Date(toIso(e.created_at)).toLocaleTimeString()), " ", eventLine(e))),
    ),
    recent.length > EVENTS_SHOWN ? el("p", { class: "muted small" }, `Showing the newest ${EVENTS_SHOWN} of ${recent.length}.`) : null,
  );
}

function artifacts(runId, files, ctx) {
  if (!files?.length) return null;
  const viewer = el("div", { class: "artifact-view" });
  const items = files.map((file) =>
    el(
      "li",
      {},
      el(
        "button",
        {
          type: "button",
          class: "linklike",
          onclick: async () => {
            replace(viewer, notice("Loading..."));
            try {
              const response = await fetch(ctx.api.artifactUrl(runId, file.path));
              const text = await response.text();
              replace(viewer, el("h3", {}, file.path), el("pre", { class: "json" }, text.length > 400000 ? `${text.slice(0, 400000)}\n...` : text));
            } catch (error) {
              replace(viewer, notice(error.message, "error"));
            }
          },
        },
        file.path,
      ),
      el("span", { class: "muted small" }, ` ${Math.ceil((file.size ?? 0) / 1024)} KB`),
    ),
  );
  return section("Artifacts", el("ul", { class: "artifacts" }, items), viewer);
}

export default {
  id: "run",
  title: "Run",
  nav: false,
  match: (path) => {
    const m = path.match(/^\/runs\/([A-Za-z0-9_-]+)$/);
    return m ? { id: m[1] } : null;
  },
  mount(root, { id }, ctx) {
    const head = el("div", { class: "page-head" });
    const body = el("div", { class: "run-body" });
    replace(root, el("p", {}, el("a", { href: "#/runs" }, "All runs")), head, body);
    let files = [];
    let filesFinal = false;
    let projectNames = null;
    let attentionKey = null;
    let attentionNode = null;
    const activity = activityPanel(id, ctx);

    async function load() {
      try {
        const detail = await ctx.api.run(id);
        const run = detail.run;
        // A live run adds files as it goes, so the listing is read on every
        // refresh while it runs and once more after it ends. A failed read
        // keeps the last listing and is tried again on the next refresh.
        if (isLive(run) || !filesFinal) {
          const listing = await ctx.api.artifacts(id).catch(() => null);
          if (listing) {
            files = listing.files ?? [];
            filesFinal = !isLive(run);
          }
        }
        // The run detail names its project only by id.
        if (projectNames === null) {
          const projects = await ctx.api.projects().catch(() => []);
          projectNames = new Map((Array.isArray(projects) ? projects : []).map((p) => [p.id, p.name]));
        }
        const actions = runActions(run).map((action) =>
          actionButton(
            ACTION_LABELS[action],
            async () => {
              await ctx.api[action](id);
              await load();
            },
            { confirm: ACTION_QUESTIONS[action], tone: action === "resume" ? "primary" : "danger" },
          ),
        );
        replace(
          head,
          el("h1", {}, run.workflow_name, " ", badge(run)),
          run.user_message ? el("p", { class: "run-message" }, run.user_message) : null,
          el("div", { class: "row" }, actions),
        );
        // Keep the same panel while the pause is the same one, so a refresh does
        // not wipe an answer that is half typed.
        const key = pauseKey(run.attention, run.metadata?.approval);
        if (key !== attentionKey) {
          attentionKey = key;
          attentionNode = key
            ? section("Needs you", attentionPanel(id, run.attention, run.metadata?.approval, ctx, () => (load(), ctx.refreshAttention())))
            : null;
        }
        const agents = agentNodes(detail.events, isLive(run));
        activity.update(agents);
        replace(
          body,
          attentionNode,
          section("Run", facts(run, projectNames.get(run.codebase_id))),
          result(run),
          nodes(run),
          agents.length ? activity.node : null,
          artifacts(id, files, ctx),
          events(detail.events),
        );
      } catch (error) {
        replace(head, el("h1", {}, "Run"), notice(error.message, "error"));
      }
    }

    load();
    const unsubscribe = ctx.live.subscribe((runIds) => {
      if (!runIds.length || runIds.includes(id)) load();
    });
    return () => {
      unsubscribe();
      activity.stop();
    };
  },
};
