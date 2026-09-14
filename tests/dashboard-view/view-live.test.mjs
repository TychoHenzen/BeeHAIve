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
    pbi_number: 3,
  });
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
