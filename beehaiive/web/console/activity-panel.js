// What a run's model sessions did: an agent node's text, thinking, tool calls
// with their output, and subtasks, from Archon's provider events. run.js keeps
// one panel for the page's life and hands it the run's agent nodes on every
// refresh, so an entry the person opened stays open. The panel fetches its own
// node's events and polls while that node runs: Archon's run stream says
// nothing while an agent node works, only when it starts and ends.
import { el, notice, replace } from "./dom.js";
import { activityCursor, activityEntries, mergeActivity } from "./model.js";

const POLL_MS = 2500;
const OUTPUT_SHOWN = 20000;
const ENTRIES_SHOWN = 300;

const clip = (text, limit) => (text.length > limit ? `${text.slice(0, limit)}\n... ${text.length - limit} more characters` : text);

// Tool output is repository and issue text: el() only ever makes text nodes.
function entryNode(entry) {
  switch (entry.kind) {
    case "attempt":
      return el("h3", { class: "activity-attempt" }, `Attempt ${entry.number}`);
    case "message":
      return el("p", { class: "activity-message" }, entry.text.trim());
    case "thought":
      return el("p", { class: "activity-thought muted" }, entry.text.trim());
    case "tool":
      return el(
        "details",
        { class: `activity-tool state-${entry.status}` },
        el("summary", {}, el("span", { class: "dot" }), el("code", {}, entry.name), el("span", { class: "activity-detail" }, entry.detail)),
        entry.subtasks.length ? el("ul", { class: "activity-subtasks small" }, entry.subtasks.map((task) => el("li", {}, task.text))) : null,
        entry.output
          ? el("pre", { class: "json" }, clip(entry.output, OUTPUT_SHOWN))
          : el("p", { class: "muted small" }, entry.status === "running" ? "Running..." : "No output."),
      );
    default:
      return el("p", { class: "activity-note muted small" }, entry.text);
  }
}

// What an entry shows now. Its node is drawn again only when this changes.
const signature = (entry) =>
  [entry.kind, entry.text?.length, entry.status, entry.output?.length, entry.subtasks?.map((task) => task.text).join("\n")].join("|");

export function activityPanel(runId, ctx) {
  const picker = el("div", { class: "row activity-picker" });
  const status = el("p", { class: "muted small" });
  const list = el("div", { class: "activity" });
  const node = el("section", { class: "panel" }, el("h2", {}, "Agent activity"), picker, status, list);
  const drawn = new Map();
  let nodes = [];
  let chosen = null;
  let pinned = false;
  let records = [];
  let timer = null;
  let busy = false;
  let again = false;
  let stopped = false;

  const stateOf = (step) => nodes.find((n) => n.step === step)?.state;

  function draw() {
    const entries = activityEntries(records);
    const shown = entries.slice(-ENTRIES_SHOWN);
    if (entries.length > shown.length) {
      replace(status, `Showing the newest ${ENTRIES_SHOWN} of ${entries.length} entries.`);
    } else if (records.length === 0) {
      replace(status, stateOf(chosen) === "running" ? "Waiting for the session's first event..." : "This node recorded no session.");
    } else {
      replace(status);
    }
    const children = shown.map((entry) => {
      const sig = signature(entry);
      const old = drawn.get(entry.key);
      if (old?.sig === sig) return old.node;
      const fresh = entryNode(entry);
      if (old?.node.open) fresh.open = true;
      drawn.set(entry.key, { sig, node: fresh });
      return fresh;
    });
    const keep = new Set(shown.map((entry) => entry.key));
    for (const key of [...drawn.keys()]) if (!keep.has(key)) drawn.delete(key);
    // Move only what changed, so an open entry and the reader's place stay put.
    children.forEach((child, index) => {
      if (list.children[index] !== child) list.insertBefore(child, list.children[index] ?? null);
    });
    while (list.children.length > children.length) list.lastElementChild.remove();
  }

  function schedule() {
    clearTimeout(timer);
    timer = null;
    if (!stopped && chosen && stateOf(chosen) === "running") timer = setTimeout(pull, POLL_MS);
  }

  async function pull() {
    if (stopped || !chosen) return;
    if (busy) {
      again = true;
      return;
    }
    busy = true;
    const step = chosen;
    try {
      const reply = await ctx.api.providerEvents(runId, step, activityCursor(records));
      if (step === chosen) {
        records = mergeActivity(records, reply?.events ?? []);
        draw();
      }
    } catch (error) {
      if (step === chosen) replace(status, notice(error.message, "error"));
    } finally {
      busy = false;
      if (again || step !== chosen) {
        again = false;
        pull();
      } else {
        schedule();
      }
    }
  }

  function drawPicker() {
    replace(
      picker,
      nodes.map((n) =>
        el(
          "button",
          { type: "button", class: n.step === chosen ? "chip active" : "chip", "aria-pressed": String(n.step === chosen), onclick: () => choose(n.step, true) },
          el("code", {}, n.step),
          " ",
          el("span", { class: `pill tone-${n.state}` }, n.state),
        ),
      ),
    );
  }

  function choose(step, byPerson) {
    if (byPerson) pinned = true;
    if (step === chosen) return;
    chosen = step;
    records = [];
    drawn.clear();
    list.replaceChildren();
    drawPicker();
    replace(status, "Loading...");
    pull();
  }

  return {
    node,
    // The run's agent nodes, newest first (model.js agentNodes). Until the
    // person picks one, the panel follows the running node, else the newest.
    update(agents) {
      const before = stateOf(chosen);
      nodes = agents;
      if (nodes.length === 0) return;
      const keep = pinned && nodes.some((n) => n.step === chosen);
      const wanted = keep ? chosen : (nodes.find((n) => n.state === "running") ?? nodes[0]).step;
      if (wanted !== chosen) {
        choose(wanted, false);
        return;
      }
      drawPicker();
      // The node ended since the last look: fetch what it wrote at its end.
      if (before === "running" && stateOf(chosen) !== "running") pull();
      else if (timer === null && !busy) schedule();
    },
    stop() {
      stopped = true;
      clearTimeout(timer);
    },
  };
}
