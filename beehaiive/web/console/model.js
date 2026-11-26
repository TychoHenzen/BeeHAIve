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

// Names one pause, so a refresh keeps a half-typed answer only while the run
// still waits at that pause. An approval gate carries no occurrence, so two
// gates in a row differ only by their node and pause. `approval` is the run's
// metadata.approval when the caller has it.
export function pauseKey(attention, approval = null) {
  if (!needsPerson(attention)) return null;
  return JSON.stringify([
    attention.kind,
    attention.occurrence ?? null,
    attention.node ?? approval?.nodeId ?? null,
    approval?.pauseId ?? attention.pausedAt ?? null,
    attention.calls?.length ?? null,
  ]);
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

// What a workflow does, in one line: the first sentence of its description.
// Descriptions are hard-wrapped YAML text, so a line break is only a space.
const SUMMARY_LIMIT = 200;
export function workflowSummary(description) {
  const paragraph = String(description ?? "").split(/\n\s*\n/)[0].replace(/\s+/g, " ").trim();
  const sentence = paragraph.match(/^.*?[.!?](?=\s|$)/)?.[0] ?? paragraph;
  return sentence.length > SUMMARY_LIMIT ? `${sentence.slice(0, SUMMARY_LIMIT).replace(/\s+\S*$/, "")}...` : sentence;
}

// The pbi workflows write every question to the owner in one shape: an
// opening paragraph, more paragraphs, lettered choices one per line ("(a)
// ...", "(b) ..."), and a sentence that recommends one. The attention page
// shows the opening, folds the rest, and turns the choices into buttons.
// ASSUMPTION: that shape is the workflow's contract (archon-home
// resolve.md "Declare", ask.mjs REPLIES); a message off it gets no buttons.
const CHOICE = /^\(([a-z])\)\s+(\S.*)$/u;
const RECOMMENDS = /\brecommend\w*\b[^()\n]{0,40}\(([a-z])\)/iu;
export const HEADLINE_LIMIT = 280;

// The opening paragraph cut to a headline, at a sentence end when one falls
// in the second half of the limit, else at a word. `rest` is what was cut.
function headlineOf(paragraph) {
  if (paragraph.length <= HEADLINE_LIMIT) return { head: paragraph, rest: "" };
  const window = paragraph.slice(0, HEADLINE_LIMIT);
  const sentence = Math.max(window.lastIndexOf(". "), window.lastIndexOf("? "));
  if (sentence >= HEADLINE_LIMIT / 2) {
    return { head: paragraph.slice(0, sentence + 1), rest: paragraph.slice(sentence + 2) };
  }
  const word = window.lastIndexOf(" ") > 0 ? window.lastIndexOf(" ") : HEADLINE_LIMIT;
  return { head: `${paragraph.slice(0, word)} ...`, rest: `... ${paragraph.slice(word + 1)}` };
}

// {headline, more, choices: [{key, says, line}], recommended, why}. Choices
// count only when they run a, b, c... in order, at least two of them; with
// `lettered: false` (a form that declares its own options) none are read.
export function questionParts(message, { lettered = true } = {}) {
  const text = String(message ?? "").replace(/\r\n?/gu, "\n").trim();
  const lines = text.split("\n");
  const found = lines.map((line) => (lettered ? CHOICE.exec(line.trim()) : null));
  const choices = found.filter(Boolean).map(([line, key, says]) => ({ key, says: says.trim(), line: line.trim() }));
  const inOrder = choices.length >= 2 && choices.every((choice, at) => choice.key === String.fromCharCode(97 + at));
  const kept = inOrder ? lines.filter((_, at) => !found[at]) : lines;
  const paragraphs = kept.join("\n").split(/\n\s*\n/u).map((part) => part.trim()).filter(Boolean);
  const keys = new Set(inOrder ? choices.map((choice) => choice.key) : []);
  const recommending = paragraphs.findLastIndex((part) => keys.has(RECOMMENDS.exec(part)?.[1]?.toLowerCase()));
  const body = paragraphs.filter((_, at) => at !== recommending || paragraphs.length === 1);
  const { head, rest } = headlineOf(body[0] ?? "");
  return {
    headline: head,
    more: [rest, ...body.slice(1)].filter(Boolean).join("\n\n"),
    choices: inOrder ? choices : [],
    recommended: recommending >= 0 ? RECOMMENDS.exec(paragraphs[recommending])[1].toLowerCase() : null,
    why: recommending >= 0 && paragraphs.length > 1 ? paragraphs[recommending] : "",
  };
}

// A choice that asks the owner to write something ("Reply with how ...") is
// not a one-tap answer: the page sends them to the text box instead.
const WRITES = /\b(?:reply|answer|say|tell|write|explain)\b[^."]*\b(?:how|what|which|why|with)\b/iu;
export function writesAnswer(choice) {
  return WRITES.test(choice.says);
}

// Questions that offer the same choices, up to issue and other numbers, are
// one question asked several times, and the page answers them together. A
// question with no choices, or not in the message form, is never grouped.
export function questionGroupKey(item) {
  if (item?.kind !== "attention" || item.form !== "message") return null;
  const { choices } = questionParts(item.message);
  if (choices.length === 0) return null;
  const said = choices.map((choice) => choice.line.toLowerCase().replace(/#?\d+/gu, "#").replace(/\s+/gu, " "));
  return JSON.stringify([item.project?.key ?? "", item.workflow ?? "", said]);
}

// The queue as groups of items that ask the same question, biggest first;
// items that share nothing stay one to a group, in queue order.
export function questionGroups(items) {
  const groups = [];
  const byKey = new Map();
  for (const item of items) {
    const key = questionGroupKey(item);
    const group = key === null ? undefined : byKey.get(key);
    if (group) group.items.push(item);
    else {
      const fresh = { key, items: [item] };
      groups.push(fresh);
      if (key !== null) byKey.set(key, fresh);
    }
  }
  // Array.prototype.sort is stable, so equal sizes keep queue order.
  return groups.sort((a, b) => b.items.length - a.items.length);
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

// dod-guard's two machine-wide workflows: one resource per host, since each
// covers every project on this machine whichever checkout it starts in. The
// pack's worktree-cleanup replaces the SDLC pack's archon-worktree-cleanup,
// which fails on submodules and long paths. The daily review reads a day of
// runs, so it runs once a day.
const perHost = (workflow, intervalSeconds, inputs) => ({
  workflow,
  perHost: true,
  intervalSeconds,
  overlap: "skip",
  capacity: 1,
  isolation: "default",
  resource: (_repo, hostId) => `${workflow}:${hostId}`,
  inputs: () => inputs,
});

// The rest are the SDLC pack's schedules, from its README.
export const BINDING_PRESETS = {
  "pbi-refine-queue": pbiQueue("refine"),
  "pbi-build-queue": pbiQueue("build"),
  "pbi-merge-queue": pbiQueue("merge"),
  "pbi-discovery-queue": pbiQueue("discovery", 14400),
  "pbi-upkeep-queue": pbiQueue("upkeep", 86400),
  "worktree-cleanup": perHost("worktree-cleanup", 21600, { include_closed: "false" }),
  "daily-review": perHost("daily-review", 86400, {}),
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
};

// A project's short name for binding names and resources: its repository's
// name, else its own. A binding name must start with a letter or digit
// (beehaiive/archon/triggers.py NAME).
export function projectSlug(project) {
  const repo = repoFromUrl(project?.repository_url);
  const raw = (repo?.path || project?.name || "").split("/").pop().toLowerCase();
  return raw.replace(/[^a-z0-9._-]+/g, "-").replace(/^[^a-z0-9]+|[^a-z0-9]+$/g, "") || "project";
}

// A binding form filled from a preset for one project.
export function presetBinding(kind, { project, directory, hostId }) {
  const preset = BINDING_PRESETS[kind];
  const repo = repoFromUrl(project?.repository_url) ?? { host: "", path: "" };
  const slug = projectSlug(project);
  const fileFor = (k) => joinPath(directory, `${slug}-${k}.json`);
  const name = `${slug}-${kind}`;
  return {
    name,
    workflow: preset.workflow,
    cwd: project?.default_cwd ?? "",
    // A repository's queue resource needs a repository; without one the
    // project's own name stands in, as it does when no preset is chosen.
    resource: suggestedResource(preset.workflow, project, hostId),
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

// The binding form's interval, in hours when it is whole hours.
export function intervalParts(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return { amount: 30, unit: "minutes" };
  return seconds % 3600 === 0 ? { amount: seconds / 3600, unit: "hours" } : { amount: seconds / 60, unit: "minutes" };
}

export function intervalSeconds({ amount, unit }) {
  return Math.round(Number(amount) * (unit === "hours" ? 3600 : 60));
}

// Windows folder names ignore case, and a trailing separator changes nothing.
const folderKey = (path) =>
  typeof path === "string" && path.trim() ? path.trim().replaceAll("/", "\\").replace(/\\+$/, "").toLowerCase() : "";

// The registered project whose folder a binding runs in, or null.
export function projectForFolder(projects, cwd) {
  const key = folderKey(cwd);
  if (!key) return null;
  return (projects ?? []).find((project) => folderKey(project?.default_cwd) === key) ?? null;
}

// The resource a new binding claims unless it shares another's: a preset's
// for a workflow a preset knows, else the workflow on its repository.
export function suggestedResource(workflow, project, hostId) {
  if (!workflow) return "";
  const repo = repoFromUrl(project?.repository_url);
  const preset = Object.values(BINDING_PRESETS).find((p) => p.workflow === workflow);
  if (preset && (repo || preset.perHost)) return preset.resource(repo, hostId);
  return `${workflow}:${repo ? `${repo.host}/${repo.path}` : projectSlug(project)}`;
}

// The resources a binding can claim: the suggested one, the one it has, then
// every resource another binding claims. Bindings on one resource share its
// capacity, so each choice says who else is on it and, when they agree, at
// what capacity.
export function resourceChoices(bindings, { self, suggested, current }) {
  const users = new Map();
  for (const binding of bindings ?? []) {
    if (!binding?.resource || binding.name === self) continue;
    if (!users.has(binding.resource)) users.set(binding.resource, []);
    users.get(binding.resource).push(binding);
  }
  const choices = [];
  const add = (value) => {
    if (!value || choices.some((choice) => choice.value === value)) return;
    const sharing = users.get(value) ?? [];
    const capacities = new Set(sharing.map((b) => b.capacity).filter(Number.isInteger));
    choices.push({ value, sharedWith: sharing.map((b) => b.name), capacity: capacities.size === 1 ? [...capacities][0] : null });
  };
  add(suggested);
  add(current);
  [...users.keys()].sort().forEach(add);
  return choices;
}

// Saved bindings on `resource` at a different capacity, the edited binding's
// own saved file included. Archon fixes a resource's capacity the first time
// a binding claims it and rejects every later fire that declares another
// (resource_capacity_conflict), so raising a binding's capacity on the
// resource it already used stops it from ever starting.
export function capacityConflicts(bindings, { resource, capacity }) {
  return (bindings ?? [])
    .filter((b) => b?.resource === resource && Number.isInteger(b.capacity) && b.capacity !== capacity)
    .map((b) => ({ name: b.name, capacity: b.capacity }));
}

// An input row's value that stands for this binding's own file, whatever the
// binding ends up named.
export const SELF_BINDING = "@self";

// The rows of a binding's inputs. Each input the workflow declares is a row,
// and one named *_binding names a binding file, so it picks from the bindings.
// An input the file has that the workflow does not declare stays as an extra
// row, so an edit never drops it. With no declaration to go by (declared is
// null), every input the file has is a plain row.
export function inputRows(declared, current, selfPath) {
  const rest = { ...(current ?? {}) };
  const rows = (declared ?? []).map((input) => {
    const value = String(rest[input.name] ?? "");
    delete rest[input.name];
    const names = input.name.endsWith("_binding");
    return { ...input, kind: names ? "binding" : "text", extra: false, value: names && selfPath && value === selfPath ? SELF_BINDING : value };
  });
  for (const [name, value] of Object.entries(rest)) {
    rows.push({ name, required: false, default: null, description: "", kind: "text", extra: declared !== null, value: String(value ?? "") });
  }
  return rows;
}

// The choices for an input that names a binding file: none (when optional),
// this binding, the others, and the value it has even if no binding has that path.
export function bindingInputOptions(bindings, { self, required, value }) {
  const options = required ? [] : [{ value: "", label: "None" }];
  options.push({ value: SELF_BINDING, label: "This binding" });
  for (const binding of bindings ?? []) {
    if (binding?.name === self || !binding?.path) continue;
    options.push({ value: binding.path, label: binding.workflow ? `${binding.name} (${binding.workflow})` : binding.name });
  }
  if (value && !options.some((option) => option.value === value)) options.push({ value, label: `${value} (no such binding)` });
  return options;
}

// The inputs a binding saves: filled rows only, so an empty optional input
// takes the workflow's default, and SELF_BINDING becomes the binding's file.
export function bindingInputs(rows, selfPath) {
  const inputs = {};
  for (const row of rows) {
    const value = row.value === SELF_BINDING ? selfPath : String(row.value ?? "").trim();
    if (value) inputs[row.name] = value;
  }
  return inputs;
}

// Whether the Windows tasks that fire bindings are paused: "none" when there
// are no tasks, "active", "partial" when some are disabled, or "paused".
export function pauseState(tasks) {
  const total = (tasks ?? []).length;
  const disabled = (tasks ?? []).filter((task) => task?.state === "Disabled").length;
  const state = total === 0 ? "none" : disabled === 0 ? "active" : disabled === total ? "paused" : "partial";
  return { state, disabled, total };
}

// A fire's run is claimed by its own archon process within seconds, so a run
// still pending with no process after this long was never going to start.
const UNCLAIMED_AFTER_SECONDS = 600;

// What each live run means for a restart. Archon names the process that
// executes a run in metadata.execution_owner ({ host, pid }).
//   running: its process is alive, so a restart would cut it off.
//   starting: pending, and its process may still claim it.
//   waiting: paused on a person's answer, which Archon's server picks up, so
//     a restart keeps it.
//   interrupted: its process is gone. It still holds its binding's resource,
//     so every later fire of that binding is skipped until it ends.
// `processes` is what /api/archon/triggers/processes said.
export function restartRuns(runs, processes, nowMs) {
  return (runs ?? [])
    .filter((run) => LIVE.has(run?.status))
    .map((run) => ({ run, binding: run.metadata?.resource_start?.bindingId ?? null, state: restartState(run, processes, nowMs) }));
}

function restartState(run, processes, nowMs) {
  // ASSUMPTION: a paused run holds no process; its answer resumes it through
  // Archon's server. No paused run existed to confirm this on 2026-11-25.
  if (run.status === "paused") return "waiting";
  const owner = run.metadata?.execution_owner;
  if (owner && processes?.supported && owner.host === processes.host && Number.isInteger(owner.pid)) {
    return processes.pids.includes(owner.pid) ? "running" : "interrupted";
  }
  if (run.status === "pending" && !owner) {
    const age = secondsBetween(run.started_at, null, nowMs);
    return age !== null && age > UNCLAIMED_AFTER_SECONDS ? "interrupted" : "starting";
  }
  // A process on another host, or one this console cannot list, is taken to be alive.
  return "running";
}

// Whether a restart is safe now: "busy" while a run is going, "unpaused" when
// none is but a task can start one, "safe" when neither.
export function restartVerdict(pause, entries) {
  const busy = entries.filter((entry) => entry.state === "running" || entry.state === "starting").length;
  if (busy > 0) return { verdict: "busy", busy };
  return { verdict: pause.state === "active" || pause.state === "partial" ? "unpaused" : "safe", busy };
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

// The Pipeline page reads archon-home's ledger (/api/archon/pipeline): each
// open issue is stuck, asking, working or queued, and each queue can run or
// cannot. Stuck issues with the same cause share one entry, so a queue that
// cannot run reads as one problem instead of thirty.

const COUNT_ORDER = ["stuck", "asking", "working", "queued", "done"];
const QUEUE_PROBLEMS = new Set(["missing", "blocked", "silent", "stalled"]);

export function pipelineCounts(counts) {
  const parts = COUNT_ORDER.filter((state) => (counts?.[state] ?? 0) > 0).map((state) => `${counts[state]} ${state}`);
  return parts.length ? parts.join(", ") : "No open issues.";
}

export function queueProblems(queues) {
  return (queues ?? []).filter((queue) => QUEUE_PROBLEMS.has(queue.state));
}

// Stuck rows grouped by their exact reason, the largest group first.
export function stuckGroups(issues) {
  const groups = new Map();
  for (const row of issues ?? []) {
    if (row.state !== "stuck") continue;
    const group = groups.get(row.detail) ?? { detail: row.detail, rows: [] };
    group.rows.push(row);
    groups.set(row.detail, group);
  }
  return [...groups.values()].sort((a, b) => b.rows.length - a.rows.length || a.rows[0].issue - b.rows[0].issue);
}

export function githubUrl(repository, kind, number) {
  return `https://github.com/${repository}/${kind === "pull" ? "pull" : "issues"}/${number}`;
}

// A long reason's opening, cut at a word, or null when it is short enough to
// show whole. Park reasons can hold a whole stack trace.
export function reasonHead(text, limit = 280) {
  const flat = String(text ?? "");
  if (flat.length <= limit) return null;
  const cut = flat.lastIndexOf(" ", limit);
  return `${flat.slice(0, cut > limit / 2 ? cut : limit)}...`;
}
