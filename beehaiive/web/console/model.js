// Pure decisions the console pages make about Archon data. No DOM here, so
// node --test covers every rule (tests/console-model.test.mjs).

// Archon writes some times as "2026-11-22 14:54:47" (UTC, no zone) and others
// as ISO. Date.parse would read the first as local time.
export function toIso(value) {
  if (typeof value !== "string" || !value) return null;
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\.\d+)?$/.test(value)) {
    return `${value.replace(" ", "T")}Z`;
  }
  return value;
}

export function secondsBetween(startIso, endIso, nowMs) {
  const start = Date.parse(toIso(startIso) ?? "");
  if (Number.isNaN(start)) return null;
  const end = endIso ? Date.parse(toIso(endIso) ?? "") : nowMs;
  if (Number.isNaN(end)) return null;
  return Math.max(0, (end - start) / 1000);
}

export const STATUS_FILTERS = ["running", "paused", "failed", "completed", "all"];

const LIVE = new Set(["pending", "running", "paused"]);

// The one word a run is shown with. A run can finish ("completed") with a
// failed outcome; that reads as failed.
export function runTone(run) {
  const status = run?.status ?? "unknown";
  if (status === "completed" && run?.outcome && run.outcome !== "succeeded") {
    return run.outcome === "failed" ? "failed" : run.outcome;
  }
  return status;
}

export function isLive(run) {
  return LIVE.has(run?.status);
}

// Which controls a run offers. Mirrors the CLI: cancel and abandon stop a live
// run; resume restarts a failed one from its completed nodes.
export function runActions(run) {
  const status = run?.status;
  if (status === "running" || status === "pending" || status === "paused") {
    return ["cancel", "abandon"];
  }
  if (status === "failed") return ["resume"];
  return [];
}

const PERSON_KINDS = new Set(["awaiting_response", "attention", "stalled_tool_calls", "unreadable"]);

export function needsPerson(attention) {
  return PERSON_KINDS.has(attention?.kind);
}

export function attentionText(attention) {
  if (!attention) return "";
  switch (attention.kind) {
    case "awaiting_response":
    case "attention":
      return attention.message ?? "Waiting for an answer.";
    case "stalled_tool_calls": {
      const count = attention.calls?.length ?? 0;
      return `${count} tool call${count === 1 ? "" : "s"} stopped making progress.`;
    }
    case "usage_limit":
      return `Waiting for the usage limit to reset${attention.resumeAt ? ` at ${attention.resumeAt}` : ""}.`;
    case "blocked_on_child":
      return "Waiting on a child run.";
    case "blocked_on_children":
      return `Waiting on ${attention.childRunIds?.length ?? 0} child runs.`;
    case "unreadable":
      return `Archon could not read this pause (${attention.reason}).`;
    case "terminal":
      return `Ended ${attention.status}.`;
    default:
      return "";
  }
}

// The decisions an approval gate offers. A gate that declares none takes the
// classic approve and reject.
export function gateDecisions(approval) {
  const declared = Array.isArray(approval?.decisions) ? approval.decisions : [];
  const usable = declared.filter((d) => d && typeof d.id === "string");
  if (usable.length > 0) {
    return usable.map((d) => ({ id: d.id, label: d.label ?? d.id, does: d.does ?? d.description ?? "" }));
  }
  return [
    { id: "approve", label: "Approve", does: "" },
    { id: "reject", label: "Reject", does: "" },
  ];
}

export function expectedGate(approval) {
  if (!approval?.nodeId) return undefined;
  return approval.pauseId ? { nodeId: approval.nodeId, pauseId: approval.pauseId } : approval.nodeId;
}

// The body for POST .../answer from the person's picks in an attention form.
export function answerBody(attention, picked, text) {
  const body = { occurrence: attention.occurrence };
  if (attention.form === "select" || attention.form === "multi_select" || attention.form === "approval") {
    body.choose = picked;
  }
  if (text && text.trim()) body.text = text;
  return body;
}

export function answerReady(attention, picked, text) {
  if (attention.form === "message") return Boolean(text && text.trim());
  if (attention.form === "select" || attention.form === "approval") return picked.length === 1;
  if (attention.form === "multi_select") return picked.length > 0;
  return false;
}

// Declared workflow inputs as a list, in declaration order.
export function workflowInputs(workflow) {
  const inputs = workflow?.inputs;
  if (!inputs || typeof inputs !== "object") return [];
  return Object.entries(inputs).map(([name, spec]) => ({
    name,
    required: spec?.required === true,
    default: typeof spec?.default === "string" ? spec.default : null,
    description: typeof spec?.description === "string" ? spec.description : "",
  }));
}

// Required inputs the person has not filled. Whitespace does not count.
export function missingRequiredInputs(declared, values) {
  return declared
    .filter((input) => input.required && (values[input.name] ?? "").trim().length === 0)
    .map((input) => input.name);
}

// Only inputs the person filled. An untouched optional field is left out, so the
// workflow's own default applies rather than an empty string.
export function collectSuppliedInputs(declared, values) {
  const supplied = {};
  for (const input of declared) {
    const value = values[input.name] ?? "";
    if (value.trim().length > 0) supplied[input.name] = value;
  }
  return supplied;
}

// "https://github.com/owner/name.git" -> { host: "github.com", path: "owner/name" }
export function repoFromUrl(url) {
  if (typeof url !== "string") return null;
  const match = url.match(/^(?:https?:\/\/|git@)([^/:]+)[/:]([^/]+\/[^/]+?)(?:\.git)?\/?$/);
  return match ? { host: match[1], path: match[2] } : null;
}

export function joinPath(directory, file) {
  const sep = directory.includes("\\") ? "\\" : "/";
  return directory.endsWith(sep) ? `${directory}${file}` : `${directory}${sep}${file}`;
}

// Each preset fills the binding form; the person can change every field before
// saving. The pbi queues come from dod-guard's Archon workflows (their README,
// "Timer bindings"): one resource per queue, capacity 1, so a queue never runs
// beside itself while the others run side by side. They take no inputs. The
// discovery queue only decides what the other three found, so it runs every
// 4 h, like the SDLC pack's discoveries. The upkeep queue files Dependabot
// alerts, which change slowly, so it runs once a day.
const pbiQueue = (queue, intervalSeconds = 1800) => ({
  workflow: `pbi-${queue}-queue`,
  intervalSeconds,
  overlap: "skip",
  capacity: 1,
  isolation: "worktree",
  resource: (repo) => `pbi:${repo.host}/${repo.path}:${queue}`,
  inputs: () => ({}),
});

// The rest are the SDLC pack's schedules, from its README.
export const BINDING_PRESETS = {
  "pbi-refine-queue": pbiQueue("refine"),
  "pbi-build-queue": pbiQueue("build"),
  "pbi-merge-queue": pbiQueue("merge"),
  "pbi-discovery-queue": pbiQueue("discovery", 14400),
  "pbi-upkeep-queue": pbiQueue("upkeep", 86400),
  "build-queue": {
    workflow: "archon-build-queue",
    intervalSeconds: 1800,
    overlap: "skip",
    capacity: 2,
    isolation: "worktree",
    resource: (repo) => `forge:${repo.host}/${repo.path}:build`,
    inputs: (repo, self) => ({ repo_host: repo.host, repo_path: repo.path, build_queue_binding: self }),
  },
  "merge-queue": {
    workflow: "archon-merge-queue",
    intervalSeconds: 1800,
    overlap: "queue",
    capacity: 1,
    // The pack's example leaves isolation out, but Archon 0.14 requires it.
    isolation: "default",
    resource: (repo) => `forge:${repo.host}/${repo.path}:merge`,
    inputs: (repo, self, sibling) => ({
      repo_host: repo.host,
      repo_path: repo.path,
      merge_queue_binding: self,
      build_queue_binding: sibling("build-queue"),
    }),
  },
  discoveries: {
    workflow: "archon-discoveries",
    intervalSeconds: 14400,
    overlap: "skip",
    capacity: 1,
    isolation: "default",
    resource: (repo) => `discoveries:${repo.host}/${repo.path}`,
    inputs: (repo) => ({ repo_host: repo.host, repo_path: repo.path }),
  },
  "worktree-cleanup": {
    workflow: "archon-worktree-cleanup",
    intervalSeconds: 21600,
    overlap: "skip",
    capacity: 1,
    isolation: "default",
    resource: (_repo, hostId) => `worktree-cleanup:${hostId}`,
    inputs: () => ({ include_closed: "false" }),
  },
};

// A binding form filled from a preset for one project.
export function presetBinding(kind, { project, directory, hostId }) {
  const preset = BINDING_PRESETS[kind];
  const repo = repoFromUrl(project?.repository_url) ?? { host: "", path: "" };
  // A binding name must start with a letter or digit (beehaiive/archon/triggers.py NAME).
  const raw = (repo.path || project?.name || "").split("/").pop().toLowerCase();
  const slug = raw.replace(/[^a-z0-9._-]+/g, "-").replace(/^[^a-z0-9]+|[^a-z0-9]+$/g, "") || "project";
  const fileFor = (k) => joinPath(directory, `${slug}-${k}.json`);
  const name = `${slug}-${kind}`;
  return {
    name,
    workflow: preset.workflow,
    cwd: project?.default_cwd ?? "",
    resource: kind === "worktree-cleanup" ? preset.resource(repo, hostId) : preset.resource(repo),
    hostId,
    intervalSeconds: preset.intervalSeconds,
    overlap: preset.overlap,
    capacity: preset.capacity,
    isolation: preset.isolation,
    inputs: preset.inputs(repo, fileFor(kind), fileFor),
  };
}

export function formatInterval(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return "";
  if (seconds % 3600 === 0) return `every ${seconds / 3600} h`;
  if (seconds % 60 === 0) return `every ${seconds / 60} min`;
  return `every ${seconds} s`;
}

// One line for a run event in the timeline.
export function eventLine(event) {
  const type = String(event?.event_type ?? "event").replaceAll("_", " ");
  const step = event?.step_name ? ` - ${event.step_name}` : "";
  const data = event?.data ?? {};
  const note = data.error ?? data.message ?? data.reason ?? data.status ?? "";
  return `${type}${step}${note ? `: ${String(note).slice(0, 200)}` : ""}`;
}

// Agent activity. Archon records each model session as provider events
// (GET /api/workflows/runs/<id>/provider-events?step=<node>): text and
// thinking in chunks, tool calls and their results, and subtasks. Each record
// is keyed by its attempt and its seq, which counts that attempt's events
// from 0. A loop runs a node once per pass, each pass its own attempt.

// The run's agent nodes, newest first, from its node events: run.nodes lists
// only the last pass of a loop, and only a node_started event says whether a
// node is an agent or a script. A node still running when the run is no
// longer live is "stopped".
export function agentNodes(events, runLive) {
  const nodes = new Map();
  for (const event of [...(events ?? [])].sort((a, b) => a.event_order - b.event_order)) {
    const step = event.step_name;
    if (event.event_type === "node_started" && step && event.data?.node?.kind === "agent") {
      nodes.delete(step);
      nodes.set(step, { step, state: "running" });
    } else if (nodes.has(step) && (event.event_type === "node_completed" || event.event_type === "node_failed")) {
      nodes.get(step).state = event.event_type === "node_completed" ? "completed" : "failed";
    }
  }
  return [...nodes.values()].reverse().map((node) => (node.state === "running" && !runLive ? { ...node, state: "stopped" } : node));
}

const recordKey = (record) => JSON.stringify([record.attemptId ?? null, record.seq]);

// Records merged without duplicates, attempts in the order first seen, each
// attempt's events by seq.
export function mergeActivity(held, incoming) {
  const attempts = [];
  const byKey = new Map();
  for (const record of [...held, ...incoming]) {
    if (!attempts.includes(record.attemptId ?? null)) attempts.push(record.attemptId ?? null);
    byKey.set(recordKey(record), record);
  }
  return [...byKey.values()].sort(
    (a, b) => attempts.indexOf(a.attemptId ?? null) - attempts.indexOf(b.attemptId ?? null) || a.seq - b.seq,
  );
}

// Where the next fetch starts: after the newest attempt's last event that
// follows an unbroken run of seq from 0. Archon commits these rows without
// waiting, so a later row can land before an earlier one; a cursor short of a
// gap fetches the gap again. null fetches everything.
export function activityCursor(records) {
  const attemptId = records.at(-1)?.attemptId;
  if (!attemptId) return null;
  let seq = -1;
  for (const record of records.filter((r) => r.attemptId === attemptId)) {
    if (record.seq !== seq + 1) break;
    seq = record.seq;
  }
  return seq < 0 ? null : { attemptId, seq };
}

const oneLine = (text, limit) => {
  const line = String(text ?? "").replace(/\s+/g, " ").trim();
  return line.length > limit ? `${line.slice(0, limit - 3)}...` : line;
};

// A session runs most commands as `cd "<its worktree>" && ...`; the cd says
// nothing the run page does not already show.
const LEADING_CD = /^cd\s+("[^"]*"|'[^']*'|\S+)\s*&&\s*/;

// What a tool call works on, in one line: the file, the command, the pattern.
export function toolDetail(input) {
  if (!input || typeof input !== "object") return "";
  const where = typeof input.path === "string" && input.pattern ? ` in ${input.path}` : "";
  const command = typeof input.command === "string" ? input.command.trim().replace(LEADING_CD, "") : undefined;
  const main = input.file_path ?? input.notebook_path ?? command ?? input.pattern ?? input.url ?? input.skill ?? input.description ?? input.query;
  return oneLine(typeof main === "string" ? `${main}${where}` : JSON.stringify(input), 200);
}

// The text a tool's recorded result holds. Archon keeps Claude's tool results
// as JSON: a shell gives stdout and stderr, a read gives file.content, a
// write or a search gives content, a file search gives filenames. Anything
// else is shown as recorded.
export function toolOutput(output) {
  if (output === undefined || output === null) return "";
  if (typeof output !== "string") return JSON.stringify(output, null, 2);
  let parsed = null;
  try {
    parsed = JSON.parse(output);
  } catch {
    return output;
  }
  if (!parsed || typeof parsed !== "object") return output;
  if (typeof parsed.stdout === "string") return [parsed.stdout, parsed.stderr].filter(Boolean).join("\n");
  if (typeof parsed.file?.content === "string") return parsed.file.content;
  if (typeof parsed.content === "string" && parsed.content) return parsed.content;
  if (Array.isArray(parsed.filenames)) return parsed.filenames.join("\n");
  return output;
}

const CHUNKS = { agent_message_chunk: "message", agent_thought_chunk: "thought" };
const QUIET = new Set(["heartbeat", "state_update"]);

// A node's records as what a person reads: text and thinking joined across
// chunks, each tool call with its result and its subtasks, and a marker
// between attempts when there are several. Every entry has a key that stays
// the same as more records arrive, so a page can keep what it drew.
export function activityEntries(records) {
  const entries = [];
  const tools = new Map();
  const tasks = new Map();
  const attempts = new Set();
  for (const { attemptId = null, seq, event } of records) {
    if (!attempts.has(attemptId)) {
      attempts.add(attemptId);
      entries.push({ key: `a:${attemptId}`, kind: "attempt", number: attempts.size });
    }
    const type = event?.type;
    const last = entries.at(-1);
    if (!type || QUIET.has(type)) continue;
    if (CHUNKS[type]) {
      if (last?.kind === CHUNKS[type]) last.text += event.text ?? "";
      else entries.push({ key: `m:${attemptId}:${seq}`, kind: CHUNKS[type], text: event.text ?? "" });
    } else if (type === "tool_call") {
      const tool = { key: `t:${event.toolCallId}`, kind: "tool", name: event.name ?? "tool", detail: toolDetail(event.rawInput), status: "running", output: "", subtasks: [] };
      tools.set(event.toolCallId, tool);
      entries.push(tool);
    } else if (type === "tool_call_update" && tools.has(event.toolCallId)) {
      const tool = tools.get(event.toolCallId);
      tool.status = event.status ?? tool.status;
      if (event.output !== undefined) tool.output = toolOutput(event.output);
    } else if (type === "subtask") {
      const text = `${event.description ?? event.taskType ?? "subtask"} (${event.status ?? "started"})`;
      const known = event.taskId ? tasks.get(event.taskId) : null;
      if (known) {
        known.text = text;
        continue;
      }
      const task = { key: `s:${event.taskId ?? `${attemptId}:${seq}`}`, kind: "note", text };
      if (event.taskId) tasks.set(event.taskId, task);
      const parent = tools.get(event.parentToolCallId);
      (parent ? parent.subtasks : entries).push(task);
    } else {
      entries.push({ key: `n:${attemptId}:${seq}`, kind: "note", text: type.replaceAll("_", " ") });
    }
  }
  return attempts.size > 1 ? entries : entries.filter((entry) => entry.kind !== "attempt");
}
