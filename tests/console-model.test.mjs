import assert from "node:assert/strict";
import { test } from "node:test";

import {
  activityCursor,
  activityEntries,
  agentNodes,
  answerBody,
  answerReady,
  attentionText,
  BINDING_PRESETS,
  collectSuppliedInputs,
  eventLine,
  expectedGate,
  fireSummary,
  formatInterval,
  githubUrl,
  gateDecisions,
  isLive,
  joinPath,
  mergeActivity,
  missingRequiredInputs,
  needsPerson,
  pauseKey,
  pipelineCounts,
  presetBinding,
  queueProblems,
  reasonHead,
  repoFromUrl,
  runActions,
  runTone,
  secondsBetween,
  stuckGroups,
  toIso,
  toolDetail,
  toolOutput,
  workflowInputs,
} from "../beehaiive/web/console/model.js";

test("toIso reads Archon's zone-less times as UTC", () => {
  assert.equal(toIso("2026-11-22 14:54:47"), "2026-11-22T14:54:47Z");
  assert.equal(toIso("2026-11-22 14:54:47.123"), "2026-11-22T14:54:47.123Z");
  assert.equal(toIso("2026-11-22T14:54:47.000Z"), "2026-11-22T14:54:47.000Z");
  assert.equal(toIso(""), null);
  assert.equal(toIso(null), null);
});

test("secondsBetween measures to now while a run is open", () => {
  const start = "2026-11-22 10:00:00";
  assert.equal(secondsBetween(start, "2026-11-22 10:01:30", 0), 90);
  assert.equal(secondsBetween(start, null, Date.parse("2026-11-22T10:00:10Z")), 10);
  assert.equal(secondsBetween("nonsense", null, 0), null);
  assert.equal(secondsBetween(start, "nonsense", 0), null);
  assert.equal(secondsBetween(start, "2026-11-22 09:00:00", 0), 0);
});

test("runTone shows a finished run with a bad outcome by its outcome", () => {
  assert.equal(runTone({ status: "running" }), "running");
  assert.equal(runTone({ status: "completed", outcome: "succeeded" }), "completed");
  assert.equal(runTone({ status: "completed", outcome: "failed" }), "failed");
  assert.equal(runTone({ status: "completed", outcome: "cancelled" }), "cancelled");
  assert.equal(runTone(null), "unknown");
  assert.equal(isLive({ status: "paused" }), true);
  assert.equal(isLive({ status: "failed" }), false);
});

test("runActions mirrors what the CLI allows", () => {
  for (const status of ["running", "pending", "paused"]) {
    assert.deepEqual(runActions({ status }), ["cancel", "abandon"]);
  }
  assert.deepEqual(runActions({ status: "failed" }), ["resume"]);
  assert.deepEqual(runActions({ status: "completed" }), []);
});

test("a pause key changes from one approval gate to the next", () => {
  const gate = { kind: "awaiting_response" };
  const first = pauseKey(gate, { nodeId: "review", pauseId: "p1" });
  // The same gate, refreshed, keeps its key, so a half-typed answer survives.
  assert.equal(pauseKey({ ...gate }, { nodeId: "review", pauseId: "p1" }), first);
  assert.notEqual(pauseKey(gate, { nodeId: "approve", pauseId: "p2" }), first);
  assert.notEqual(pauseKey(gate, { nodeId: "review", pauseId: "p3" }), first);
  // A queue item has no approval metadata, so its node and pause time tell gates apart.
  const queued = (node, pausedAt) => pauseKey({ ...gate, node, pausedAt });
  assert.equal(queued("review", "t1"), queued("review", "t1"));
  assert.notEqual(queued("review", "t1"), queued("approve", "t2"));
  assert.equal(pauseKey({ kind: "usage_limit" }), null);
});

test("only some pauses need a person, and each has a sentence", () => {
  assert.equal(needsPerson({ kind: "attention" }), true);
  assert.equal(needsPerson({ kind: "usage_limit" }), false);
  assert.equal(needsPerson(null), false);
  assert.equal(attentionText({ kind: "attention", message: "Pick one" }), "Pick one");
  assert.equal(attentionText({ kind: "awaiting_response" }), "Waiting for an answer.");
  assert.equal(attentionText({ kind: "stalled_tool_calls", calls: [{}] }), "1 tool call stopped making progress.");
  assert.equal(attentionText({ kind: "stalled_tool_calls", calls: [{}, {}] }), "2 tool calls stopped making progress.");
  assert.match(attentionText({ kind: "usage_limit", resumeAt: "noon" }), /at noon/);
  assert.equal(attentionText({ kind: "usage_limit" }), "Waiting for the usage limit to reset.");
  assert.equal(attentionText({ kind: "blocked_on_child" }), "Waiting on a child run.");
  assert.equal(attentionText({ kind: "blocked_on_children", childRunIds: ["a", "b"] }), "Waiting on 2 child runs.");
  assert.match(attentionText({ kind: "unreadable", reason: "bad json" }), /bad json/);
  assert.equal(attentionText({ kind: "terminal", status: "failed" }), "Ended failed.");
  assert.equal(attentionText({ kind: "other" }), "");
  assert.equal(attentionText(null), "");
});

test("a gate offers its declared decisions, or approve and reject", () => {
  assert.deepEqual(gateDecisions({ decisions: [{ id: "ship", description: "merge it" }, { nope: 1 }] }), [
    { id: "ship", label: "ship", does: "merge it" },
  ]);
  assert.deepEqual(
    gateDecisions(null).map((d) => d.id),
    ["approve", "reject"],
  );
  assert.deepEqual(expectedGate({ nodeId: "n1", pauseId: "p1" }), { nodeId: "n1", pauseId: "p1" });
  assert.equal(expectedGate({ nodeId: "n1" }), "n1");
  assert.equal(expectedGate({}), undefined);
});

test("answers carry the occurrence and only what the form takes", () => {
  const select = { form: "select", occurrence: 3 };
  assert.deepEqual(answerBody(select, ["a"], "  "), { occurrence: 3, choose: ["a"] });
  assert.deepEqual(answerBody({ form: "message", occurrence: 1 }, [], "hi"), { occurrence: 1, text: "hi" });
  assert.equal(answerReady(select, [], ""), false);
  assert.equal(answerReady(select, ["a"], ""), true);
  assert.equal(answerReady({ form: "multi_select" }, ["a", "b"], ""), true);
  assert.equal(answerReady({ form: "message" }, [], " "), false);
  assert.equal(answerReady({ form: "unknown" }, ["a"], "x"), false);
});

test("workflow inputs keep declaration order and leave untouched fields out", () => {
  const declared = workflowInputs({
    inputs: { issue: { required: true, description: "Which issue" }, branch: { default: "main" }, odd: null },
  });
  assert.deepEqual(
    declared.map((i) => [i.name, i.required, i.default]),
    [
      ["issue", true, null],
      ["branch", false, "main"],
      ["odd", false, null],
    ],
  );
  assert.deepEqual(workflowInputs({}), []);
  assert.deepEqual(missingRequiredInputs(declared, { issue: "  " }), ["issue"]);
  assert.deepEqual(collectSuppliedInputs(declared, { issue: "42", branch: "" }), { issue: "42" });
});

test("repoFromUrl reads https and ssh remotes", () => {
  assert.deepEqual(repoFromUrl("https://github.com/TychoHenzen/BeeHAIve.git"), { host: "github.com", path: "TychoHenzen/BeeHAIve" });
  assert.deepEqual(repoFromUrl("git@github.com:o/r.git"), { host: "github.com", path: "o/r" });
  assert.equal(repoFromUrl("not a url"), null);
  assert.equal(repoFromUrl(undefined), null);
});

test("joinPath keeps the folder's own separator", () => {
  assert.equal(joinPath("C:\\Users\\me\\.archon\\triggers", "a.json"), "C:\\Users\\me\\.archon\\triggers\\a.json");
  assert.equal(joinPath("/home/me/triggers/", "a.json"), "/home/me/triggers/a.json");
});

test("a build queue preset names its own file and the repository", () => {
  const project = { name: "BeeHAIve", default_cwd: "C:\\src\\BeeHAIve", repository_url: "https://github.com/o/BeeHAIve" };
  const filled = presetBinding("build-queue", { project, directory: "C:\\h\\triggers", hostId: "local" });
  assert.equal(filled.name, "beehaive-build-queue");
  assert.equal(filled.workflow, "archon-build-queue");
  assert.equal(filled.resource, "forge:github.com/o/BeeHAIve:build");
  assert.equal(filled.cwd, "C:\\src\\BeeHAIve");
  assert.deepEqual(filled.inputs, {
    repo_host: "github.com",
    repo_path: "o/BeeHAIve",
    build_queue_binding: "C:\\h\\triggers\\beehaive-build-queue.json",
  });
});

test("a merge queue preset points at its sibling build queue", () => {
  const project = { name: "x", default_cwd: "/x", repository_url: "git@github.com:o/x.git" };
  const filled = presetBinding("merge-queue", { project, directory: "/t", hostId: "local" });
  assert.equal(filled.overlap, "queue");
  assert.equal(filled.isolation, "default");
  assert.equal(filled.inputs.merge_queue_binding, "/t/x-merge-queue.json");
  assert.equal(filled.inputs.build_queue_binding, "/t/x-build-queue.json");
});

test("the cleanup and daily review presets are per machine and every preset has a workflow", () => {
  const filled = presetBinding("worktree-cleanup", { project: { name: "My Repo!" }, directory: "/t", hostId: "pc1" });
  assert.equal(filled.workflow, "worktree-cleanup");
  assert.equal(filled.resource, "worktree-cleanup:pc1");
  assert.equal(filled.name, "my-repo-worktree-cleanup");
  assert.deepEqual(filled.inputs, { include_closed: "false" });
  const project = { name: "archon-home", default_cwd: "C:\\src\\archon-home", repository_url: "https://github.com/o/archon-home" };
  const review = presetBinding("daily-review", { project, directory: "/t", hostId: "pc1" });
  assert.deepEqual(
    [review.name, review.workflow, review.resource, review.intervalSeconds, review.capacity, review.overlap, review.isolation, review.cwd],
    ["archon-home-daily-review", "daily-review", "daily-review:pc1", 86400, 1, "skip", "default", "C:\\src\\archon-home"],
  );
  assert.deepEqual(review.inputs, {});
  assert.equal(presetBinding("discoveries", { project: { name: "--" }, directory: "/t", hostId: "h" }).name, "project-discoveries");
  assert.equal(filled.cwd, "");
  for (const preset of Object.values(BINDING_PRESETS)) {
    assert.match(preset.workflow, /^[a-z][a-z0-9-]+$/);
    assert.ok(preset.intervalSeconds >= 60);
  }
  assert.equal(presetBinding("discoveries", { project: null, directory: "/t", hostId: "h" }).name, "project-discoveries");
});

test("each pbi queue preset gets its own resource, one run at a time and no inputs", () => {
  const project = { name: "CardCleaner", default_cwd: "C:\\src\\cc", repository_url: "https://github.com/o/CardCleaner" };
  const filled = ["refine", "build", "merge", "discovery", "upkeep"].map((queue) =>
    presetBinding(`pbi-${queue}-queue`, { project, directory: "C:\\h\\triggers", hostId: "local" }),
  );
  assert.deepEqual(
    filled.map(({ name, workflow, resource, intervalSeconds }) => [name, workflow, resource, intervalSeconds]),
    [
      ["cardcleaner-pbi-refine-queue", "pbi-refine-queue", "pbi:github.com/o/CardCleaner:refine", 1800],
      ["cardcleaner-pbi-build-queue", "pbi-build-queue", "pbi:github.com/o/CardCleaner:build", 1800],
      ["cardcleaner-pbi-merge-queue", "pbi-merge-queue", "pbi:github.com/o/CardCleaner:merge", 1800],
      ["cardcleaner-pbi-discovery-queue", "pbi-discovery-queue", "pbi:github.com/o/CardCleaner:discovery", 14400],
      ["cardcleaner-pbi-upkeep-queue", "pbi-upkeep-queue", "pbi:github.com/o/CardCleaner:upkeep", 86400],
    ],
  );
  for (const binding of filled) {
    assert.equal(binding.capacity, 1);
    assert.equal(binding.overlap, "skip");
    assert.equal(binding.isolation, "worktree");
    assert.equal(binding.cwd, "C:\\src\\cc");
    assert.deepEqual(binding.inputs, {});
  }
});

test("fireSummary makes a refused fire the card's error and leaves the rest quiet", () => {
  const stuck = { state: "stuck", detail: "Archon holds r at capacity 1.", receivedAt: "2026-11-25T18:58:30.267Z" };
  assert.deepEqual(fireSummary(stuck), { tone: "error", lead: "Stuck: Archon refused its last fire", detail: "Archon holds r at capacity 1." });
  assert.deepEqual(fireSummary({ state: "ok", detail: "Archon started its workflow.", receivedAt: "2026-11-25T18:58:30.267Z" }), {
    tone: "muted",
    lead: "Last fire",
    detail: "Archon started its workflow.",
  });
  assert.deepEqual(fireSummary({ state: "quiet", detail: "No recent fire." }), { tone: "muted", lead: "", detail: "No recent fire." });
  assert.equal(fireSummary(undefined), null);
});

test("formatInterval picks the largest whole unit", () => {
  assert.equal(formatInterval(1800), "every 30 min");
  assert.equal(formatInterval(14400), "every 4 h");
  assert.equal(formatInterval(90), "every 90 s");
  assert.equal(formatInterval(0), "");
  assert.equal(formatInterval(null), "");
});

test("eventLine names the event, its step and the reason", () => {
  assert.equal(eventLine({ event_type: "node_failed", step_name: "build", data: { error: "boom" } }), "node failed - build: boom");
  assert.equal(eventLine({ event_type: "workflow_started" }), "workflow started");
  assert.equal(eventLine({}), "event");
  assert.equal(eventLine({ event_type: "x", data: { message: "m".repeat(300) } }).length, "x: ".length + 200);
});

// Provider event records in the shape Archon 0.14 serves them, trimmed from a
// pbi-refine-queue run's refine node.
const record = (seq, event, attemptId = "a1") => ({ runId: "r", stepName: "steps.root__refine", attemptId, seq, observedAt: "", event });
const quiet = { type: "heartbeat" };

test("agentNodes lists the agent nodes newest first from their start events", () => {
  const event = (order, type, step, kind) => ({ event_order: order, event_type: type, step_name: step, data: kind ? { node: { kind } } : {} });
  const events = [
    event(6, "node_started", "steps.root__finish", "agent"),
    event(1, "node_started", "steps.root__route", "exec"),
    event(2, "node_completed", "steps.root__route", "exec"),
    event(3, "node_started", "steps.root__refine", "agent"),
    event(4, "node_skipped", "steps.root__start"),
    event(5, "node_completed", "steps.root__refine", "agent"),
  ];
  assert.deepEqual(agentNodes(events, true), [
    { step: "steps.root__finish", state: "running" },
    { step: "steps.root__refine", state: "completed" },
  ]);
  assert.equal(agentNodes(events, false)[0].state, "stopped");
  assert.deepEqual(agentNodes(undefined, true), []);
});

test("activityEntries joins chunks and pairs each tool call with its result and subtasks", () => {
  const records = [
    record(0, { type: "state_update", state: "running" }),
    record(1, { type: "agent_message_chunk", text: "I'll start by reading " }),
    record(2, { type: "agent_message_chunk", text: "the stage rules." }),
    record(3, { type: "tool_call", toolCallId: "t1", name: "Bash", rawInput: { command: "gh issue view 452\n  --json body", description: "Read it" } }),
    record(4, quiet),
    record(5, { type: "subtask", taskId: "k1", status: "started", description: "Check linked PRs", parentToolCallId: "t1" }),
    record(6, { type: "tool_call_update", toolCallId: "t1", status: "completed", output: JSON.stringify({ stdout: "body", stderr: "warn" }) }),
    record(7, { type: "subtask", taskId: "k1", status: "completed", description: "Check linked PRs", parentToolCallId: "t1" }),
    record(8, { type: "agent_thought_chunk", text: "Entries 2 and 3 are fixed." }),
    record(9, { type: "tool_call", toolCallId: "t2", name: "Grep", rawInput: { pattern: "select-next", path: "src" } }),
  ];
  assert.deepEqual(activityEntries(records), [
    { key: "m:a1:1", kind: "message", text: "I'll start by reading the stage rules." },
    {
      key: "t:t1",
      kind: "tool",
      name: "Bash",
      detail: "gh issue view 452 --json body",
      status: "completed",
      output: "body\nwarn",
      subtasks: [{ key: "s:k1", kind: "note", text: "Check linked PRs (completed)" }],
    },
    { key: "m:a1:8", kind: "thought", text: "Entries 2 and 3 are fixed." },
    { key: "t:t2", kind: "tool", name: "Grep", detail: "select-next in src", status: "running", output: "", subtasks: [] },
  ]);
});

test("a node a loop ran twice shows each pass as an attempt", () => {
  const records = [record(0, { type: "agent_message_chunk", text: "one" }, "a1"), record(0, { type: "agent_message_chunk", text: "two" }, "a2")];
  assert.deepEqual(
    activityEntries(records).map(({ key, kind }) => `${kind} ${key}`),
    ["attempt a:a1", "message m:a1:0", "attempt a:a2", "message m:a2:0"],
  );
});

test("the next fetch starts before a missing event, and merging drops what is already held", () => {
  const held = [record(0, quiet), record(1, quiet), record(0, quiet, "a2"), record(1, quiet, "a2"), record(3, quiet, "a2")];
  assert.deepEqual(activityCursor(held), { attemptId: "a2", seq: 1 });
  assert.equal(activityCursor([]), null);
  assert.equal(activityCursor([record(2, quiet)]), null);
  const merged = mergeActivity(held, [record(2, quiet, "a2"), record(3, quiet, "a2"), record(0, quiet, "a3")]);
  assert.deepEqual(
    merged.map(({ attemptId, seq }) => `${attemptId}:${seq}`),
    ["a1:0", "a1:1", "a2:0", "a2:1", "a2:2", "a2:3", "a3:0"],
  );
  assert.deepEqual(activityCursor(merged), { attemptId: "a3", seq: 0 });
});

test("a tool call reads as what it works on, and shell output as its text", () => {
  assert.equal(toolDetail({ file_path: "C:\src\stage-rules.md" }), "C:\src\stage-rules.md");
  assert.equal(toolDetail({ skill: "dod-guard:refine-backlog-item", args: "452" }), "dod-guard:refine-backlog-item");
  assert.equal(toolDetail({ answer: 42 }), '{"answer":42}');
  assert.equal(toolDetail(null), "");
  assert.equal(toolDetail({ command: "x".repeat(300) }).length, 200);
  assert.equal(toolDetail({ command: 'cd "C:/work tree" && gh issue view 452' }), "gh issue view 452");
  assert.equal(toolDetail({ command: "cd src && node --test" }), "node --test");
  assert.equal(toolOutput("Structured output provided successfully"), "Structured output provided successfully");
  assert.equal(toolOutput(JSON.stringify({ stdout: "", stderr: "boom" })), "boom");
  assert.equal(toolOutput(JSON.stringify({ type: "text", file: { filePath: "a.md", content: "# A" } })), "# A");
  assert.equal(toolOutput(JSON.stringify({ mode: "files_with_matches", content: "", filenames: ["a.js", "b.js"] })), "a.js\nb.js");
  assert.equal(toolOutput(JSON.stringify({ mode: "content", content: "a.js:3: hit", filenames: [] })), "a.js:3: hit");
  assert.equal(toolOutput("[1, 2]"), "[1, 2]");
  assert.equal(toolOutput({ ok: true }), '{\n  "ok": true\n}');
  assert.equal(toolOutput(undefined), "");
});

test("pipelineCounts names each nonzero state, stuck first", () => {
  assert.equal(pipelineCounts({ stuck: 48, asking: 0, working: 1, queued: 1, done: 6 }), "48 stuck, 1 working, 1 queued, 6 done");
  assert.equal(pipelineCounts({ stuck: 0, asking: 0, working: 0, queued: 0, done: 0 }), "No open issues.");
  assert.equal(pipelineCounts(undefined), "No open issues.");
});

test("queueProblems keeps the queues that cannot run", () => {
  const queues = ["working", "stalled", "idle", "blocked", "silent", "missing"].map((state) => ({ queue: state, state }));
  assert.deepEqual(
    queueProblems(queues).map((queue) => queue.state),
    ["stalled", "blocked", "silent", "missing"],
  );
  assert.deepEqual(queueProblems(undefined), []);
});

test("stuckGroups shares one entry per reason, the largest first", () => {
  const row = (issue, state, detail) => ({ issue, state, detail });
  const groups = stuckGroups([
    row(477, "stuck", "parked, and nothing is asking you: which format?"),
    row(502, "stuck", "the refine queue cannot run"),
    row(503, "stuck", "the refine queue cannot run"),
    row(491, "working", "run cb2356da took it"),
    row(452, "stuck", "the refine queue cannot run"),
  ]);
  assert.deepEqual(
    groups.map((group) => [group.detail, group.rows.map((r) => r.issue)]),
    [
      ["the refine queue cannot run", [502, 503, 452]],
      ["parked, and nothing is asking you: which format?", [477]],
    ],
  );
});

test("githubUrl links an issue or a pull request", () => {
  assert.equal(githubUrl("o/repo", "issue", 7), "https://github.com/o/repo/issues/7");
  assert.equal(githubUrl("o/repo", "pull", 8), "https://github.com/o/repo/pull/8");
});

test("reasonHead cuts a long reason at a word and leaves a short one whole", () => {
  assert.equal(reasonHead("short reason"), null);
  assert.equal(reasonHead("alpha beta gamma delta", 12), "alpha beta...");
  assert.equal(reasonHead("x".repeat(30), 12), `${"x".repeat(12)}...`);
});
