import assert from "node:assert/strict";
import test from "node:test";

import { createDashboardView } from "../../docs/dashboard-view.mjs";
import { FakeDocument } from "./fixtures/fake-document.mjs";
import { FakeNode } from "./fixtures/fake-node.mjs";
import { findNode } from "./fixtures/helpers.mjs";

test("rendering live state exposes current stages and active actions", () => {
  const summaryOutput = new FakeNode("section");
  const dashboardOutput = new FakeNode("section");
  const actionLog = new FakeNode("section");
  const actionsOutput = new FakeNode("div");
  const actionPayloads = [];
  const view = createDashboardView({
    document: new FakeDocument(),
    summaryOutput,
    dashboardOutput,
    actionLog,
    actionsOutput,
    runAction: (payload) => actionPayloads.push(payload),
  });

  view.render({
    name: "Planning",
    updated_at: "2026-09-09T20:00:00Z",
    counts: {
      projects: 1,
      active_repositories: 1,
      repositories: 2,
      pbis: 2,
      subtasks: 1,
      writers: 1,
      readers: 1,
      active_runs: 1,
      failed_runs: 0,
    },
    scheduler: {
      enabled: true,
      running: true,
      active_workers: 1,
      max_concurrency: 2,
      last_poll_at: "2026-09-09T20:00:00Z",
    },
    repositories: [
      {
        name: "owner/api",
        active: true,
        writer: { status: "idle" },
        pbis: [
          {
            id: "owner/api#1",
            number: 1,
            title: "Live API",
            status: "pending",
            stage_progress: [
              { label: "Backlog", status: "complete" },
              { label: "Pull request", status: "current" },
            ],
            task_contract: {
              contract_id: "test.contract",
              version: 1,
              step_id: "inspect",
              inputs: { repository: "owner/api", pbi_number: 1 },
              capabilities: ["read_repository"],
              allowed_outcomes: ["pass", "fail", "blocked", "question"],
              required_evidence: ["summary"],
              required_artifacts: [
                { id: "report", description: "Inventory report", required: true },
              ],
            },
            task_result: {
              outcome: "pass",
              evidence: { summary: "done" },
              artifact_refs: [{ id: "report", path: "report.txt" }],
            },
            agent_session: {
              session_id: "run-1",
              worker_id: "worker-1",
              task: "inspect repository",
              state: "active",
              events: [
                {
                  sequence: 1,
                  kind: "progress",
                  source_type: "turn.started",
                  timestamp: "2026-09-09T19:59:00Z",
                  text: "started",
                },
                {
                  sequence: 2,
                  kind: "message",
                  source_type: "agent_message",
                  role: "assistant",
                  timestamp: "2026-09-09T19:59:30Z",
                  text: "safe <message>",
                },
              ],
            },
            graph_trace: [{
              execution_id: "run-1",
              revision: 1,
              node_id: "start",
              step: 1,
              attempt: 1,
              outcome: "pass",
              status: "advanced",
              selected_edge: { target: "done" },
              reason: "next node",
              created_at: "2026-09-09T19:59:45Z",
              evidence: { summary: "safe" },
            }, {
              execution_id: "run-1",
              revision: 1,
              node_id: "done",
              step: 2,
              attempt: 1,
              outcome: "pass",
              status: "terminal",
              reason: "complete",
              created_at: "2026-09-09T19:59:50Z",
              evidence: { summary: "finished" },
            }],
            subtasks: [{ id: "#2", title: "Test API" }],
            escalation: { current: 0, current_tier: "terra", consecutive: 0 },
            escalation_log: [{ tier: "terra", resolved: false }],
            readers: [],
            reviewers: {},
            activity: [],
            result: "inventory complete",
          },
        ],
      },
      {
        name: "owner/web",
        active: true,
        writer: { status: "active", current_pbi: 3 },
        pbis: [
          {
            id: "owner/web#3",
            number: 3,
            title: "Active web run",
            status: "active",
            run_id: "run-3",
            stage_progress: [],
            subtasks: [],
            readers: [],
            reviewers: {},
            activity: [],
          },
        ],
      },
    ],
    actions: [{ kind: "approve", status: "succeeded", repository: "owner/web" }],
  });

  assert.match(summaryOutput.textContent, /Projects1/);
  assert.match(summaryOutput.textContent, /Schedulerenabled, running/);
  assert.match(summaryOutput.textContent, /Worker capacity1 \/ 2/);
  assert.match(summaryOutput.textContent, /Last poll2026-09-09T20:00:00Z/);
  assert.match(dashboardOutput.textContent, /Planning/);
  assert.match(dashboardOutput.textContent, /Updated: 2026-09-09T20:00:00Z/);
  assert.match(dashboardOutput.textContent, /Live API/);
  assert.match(dashboardOutput.textContent, /Pull request/);
  assert.match(dashboardOutput.textContent, /Tier terra/);
  assert.match(dashboardOutput.textContent, /Result: inventory complete/);
  assert.match(dashboardOutput.textContent, /Task outcome: pass/);
  assert.match(dashboardOutput.textContent, /Inputs:.*owner\/api/);
  assert.match(dashboardOutput.textContent, /Allowed outcomes:.*question/);
  assert.match(dashboardOutput.textContent, /Required evidence:.*summary/);
  assert.match(dashboardOutput.textContent, /Inventory report/);
  assert.match(dashboardOutput.textContent, /Artifacts:/);
  assert.match(dashboardOutput.textContent, /Agent session/);
  assert.match(dashboardOutput.textContent, /Worker: worker-1/);
  assert.match(dashboardOutput.textContent, /Task: inspect repository/);
  assert.match(dashboardOutput.textContent, /State: active/);
  assert.match(dashboardOutput.textContent, /started/);
  assert.match(dashboardOutput.textContent, /safe <message>/);
  assert.match(dashboardOutput.textContent, /Graph trace/);
  assert.match(dashboardOutput.textContent, /node start -> done/);
  assert.match(dashboardOutput.textContent, /run run-1/);
  const traceFilter = findNode(
    dashboardOutput,
    (node) => node.tag === "select",
  );
  assert.ok(traceFilter);
  const traceDetails = findNode(
    dashboardOutput,
    (node) => node.tag === "details",
  );
  assert.ok(traceDetails);
  traceFilter.value = "paused";
  traceFilter.listeners.get("change")();
  assert.match(dashboardOutput.textContent, /No graph trace events match this filter/);
  traceFilter.value = "terminal";
  traceFilter.listeners.get("change")();
  assert.doesNotMatch(dashboardOutput.textContent, /step 1/);
  assert.match(dashboardOutput.textContent, /step 2/);
  assert.equal(actionLog.hidden, false);

  const startButton = findNode(
    dashboardOutput,
    (node) => node.tag === "button" && node._textContent === "Start writer",
  );
  assert.ok(startButton);
  startButton.click();
  assert.deepEqual(actionPayloads, [{ action: "start", repository: "owner/api" }]);

  const stopButton = findNode(
    dashboardOutput,
    (node) => node.tag === "button" && node._textContent === "Stop",
  );
  assert.ok(stopButton);
  stopButton.click();
  assert.deepEqual(actionPayloads.at(-1), {
    action: "stop",
    run_id: "run-3",
    repository: "owner/web",
  });
});

test("session and scheduler display stay bounded and fail closed on missing values", () => {
  const summaryOutput = new FakeNode("section");
  const dashboardOutput = new FakeNode("section");
  const actionLog = new FakeNode("section");
  const actionsOutput = new FakeNode("div");
  const view = createDashboardView({
    document: new FakeDocument(),
    summaryOutput,
    dashboardOutput,
    actionLog,
    actionsOutput,
    runAction: () => {},
  });
  const events = Array.from({ length: 101 }, (_, sequence) => ({
    sequence,
    kind: "message",
    text: sequence === 100 ? "x".repeat(5_000) : `event-${sequence}`,
  }));

  view.render({
    name: "Planning",
    counts: {},
    scheduler: { enabled: true, running: false, active_workers: 0 },
    repositories: [
      {
        name: "owner/api",
        active: true,
        writer: { status: "idle" },
        pbis: [
          {
            number: 1,
            title: "Bounded API",
            status: "active",
            agent_session: { worker_id: "worker-1", task: "inspect", events },
            stage_progress: [],
            subtasks: [],
            readers: [],
            reviewers: {},
            activity: [],
          },
        ],
      },
    ],
  });

  assert.match(summaryOutput.textContent, /Schedulerenabled, stopped/);
  assert.match(summaryOutput.textContent, /Worker capacity0 \/ Unavailable/);
  assert.doesNotMatch(dashboardOutput.textContent, /event-0/);
  assert.match(dashboardOutput.textContent, /event-99/);
  assert.equal(dashboardOutput.textContent.includes("x".repeat(4_001)), false);
});

test("rendering keeps project and pull-request terminal state visible", () => {
  const summaryOutput = new FakeNode("section");
  const dashboardOutput = new FakeNode("section");
  const actionLog = new FakeNode("section");
  const actionsOutput = new FakeNode("div");
  const view = createDashboardView({
    document: new FakeDocument(),
    summaryOutput,
    dashboardOutput,
    actionLog,
    actionsOutput,
    runAction: () => {},
  });

  view.render({
    name: "Planning",
    counts: {},
    repositories: [
      {
        name: "owner/api",
        active: true,
        writer: { status: "idle" },
        pbis: [
          {
            id: "owner/api#1",
            number: 1,
            title: "Done PBI",
            status: "idle",
            planning_status: "Done",
            stage_progress: [{ label: "Merged", status: "current" }],
            pull_requests: [{ number: 9, state: "closed", merged: true }],
            subtasks: [],
            readers: [],
            reviewers: {},
            activity: [],
          },
          {
            id: "owner/api#2",
            number: 2,
            title: "Open PBI",
            status: "idle",
            planning_status: "In Progress",
            stage_progress: [{ label: "Implement", status: "current" }],
            pull_requests: [
              { number: 10, state: "open" },
              { number: 11, state: "closed", review_decision: "changes_requested" },
            ],
            subtasks: [],
            readers: [],
            reviewers: {},
            activity: [],
          },
        ],
      },
    ],
  });

  assert.match(dashboardOutput.textContent, /Project status: Done/);
  assert.match(dashboardOutput.textContent, /#9: merged/);
  assert.match(dashboardOutput.textContent, /#10: open, review pending/);
  assert.match(dashboardOutput.textContent, /#11: closed, changes_requested/);
  assert.doesNotMatch(dashboardOutput.textContent, /#9: review pending/);
});

test("rendering exposes the canonical lifecycle state and evidence", () => {
  const dashboardOutput = new FakeNode("section");
  const view = createDashboardView({
    document: new FakeDocument(),
    summaryOutput: new FakeNode("section"),
    dashboardOutput,
    actionLog: new FakeNode("section"),
    actionsOutput: new FakeNode("div"),
    runAction: () => {},
  });
  view.render({
    name: "Planning",
    counts: {},
    repositories: [
      {
        name: "owner/api",
        active: true,
        writer: { status: "idle" },
        pbis: [
          {
            number: 1,
            title: "Blocked API",
            canonical_lifecycle: {
              state: "blocked",
              facts: { project: {}, provider: {}, run: {}, optional_sources: {} },
              source_version: "v1",
              reason_code: "conflict",
              required_action: "Refresh checks",
              transition_evidence: [
                {
                  state_before: "checks",
                  state_after: "blocked",
                  reason_code: "conflict",
                  source_id: "owner/api#1",
                  observed_at: "2026-09-15T00:00:00+00:00",
                },
              ],
            },
            stage_progress: [],
            subtasks: [],
            readers: [],
            reviewers: {},
            activity: [],
          },
        ],
      },
    ],
  });

  assert.match(dashboardOutput.textContent, /Canonical lifecycle/);
  assert.match(dashboardOutput.textContent, /State: blocked/);
  assert.match(dashboardOutput.textContent, /Reason: conflict/);
  assert.match(dashboardOutput.textContent, /Required action: Refresh checks/);
  assert.match(dashboardOutput.textContent, /Facts: project, provider, run, optional_sources/);
  assert.match(dashboardOutput.textContent, /checks -> blocked/);
  assert.match(dashboardOutput.textContent, /source owner\/api#1/);
});

test("rendering a pending operator question exposes its safe state and answer action", () => {
  const summaryOutput = new FakeNode("section");
  const dashboardOutput = new FakeNode("section");
  const actionLog = new FakeNode("section");
  const actionsOutput = new FakeNode("div");
  const actionPayloads = [];
  const view = createDashboardView({
    document: new FakeDocument(),
    summaryOutput,
    dashboardOutput,
    actionLog,
    actionsOutput,
    runAction: (payload) => actionPayloads.push(payload),
  });

  view.render({
    name: "Planning",
    counts: { awaiting_operator_runs: 1 },
    repositories: [
      {
        name: "owner/api",
        active: true,
        writer: { status: "idle" },
        pbis: [
          {
            id: "owner/api#1",
            number: 1,
            title: "Blocked API run",
            status: "awaiting_operator",
            run_id: "run-1",
            operator_questions: [
              {
                question_id: "question-1",
                revision: 2,
                kind: "question",
                status: "pending",
                question: "Which branch should be used?",
                owner_scope: "project-1",
                notification_status: "pending",
                notification_attempts: 0,
                evidence: { check: "branch name" },
              },
            ],
            stage_progress: [],
            subtasks: [],
            readers: [],
            reviewers: {},
            activity: [],
          },
        ],
      },
    ],
  });

  assert.match(summaryOutput.textContent, /Waiting for operator1/);
  assert.match(dashboardOutput.textContent, /Which branch should be used/);
  assert.match(dashboardOutput.textContent, /question-1/);
  assert.match(dashboardOutput.textContent, /Notification: pending/);
  const originalWindow = globalThis.window;
  globalThis.window = { prompt: () => "main" };
  try {
    const answer = findNode(
      dashboardOutput,
      (node) => node.tag === "button" && node._textContent === "Answer operator question",
    );
    assert.ok(answer);
    answer.click();
  } finally {
    if (originalWindow === undefined) delete globalThis.window;
    else globalThis.window = originalWindow;
  }
  assert.deepEqual(actionPayloads, [
    {
      action: "answer_question",
      repository: "owner/api",
      pbi_number: 1,
      run_id: "run-1",
      question_id: "question-1",
      revision: 2,
      answer: "main",
    },
  ]);

  view.render({
    name: "Planning",
    repositories: [
      {
        name: "owner/api",
        active: true,
        writer: { status: "idle" },
        pbis: [
          {
            id: "owner/api#1",
            number: 1,
            title: "Completed API run",
            status: "completed",
            run_id: "run-1",
            operator_questions: [
              {
                question_id: "question-1",
                revision: 2,
                kind: "question",
                status: "answered",
                question: "Which branch should be used?",
                answer: "main",
                notification_status: "cancelled",
                notification_attempts: 0,
              },
            ],
            task_result: {
              outcome: "pass",
              evidence: { summary: "Final implementation verified" },
              artifact_refs: [],
            },
            stage_progress: [],
            subtasks: [],
            readers: [],
            reviewers: {},
            activity: [],
          },
        ],
      },
    ],
  });
  assert.match(dashboardOutput.textContent, /Task outcome: pass/);
  const notification = findNode(
    dashboardOutput,
    (node) => node.tag === "div" && node._textContent.startsWith("Notification: cancelled"),
  );
  assert.ok(notification);
  assert.equal(notification.className, "muted");
});
