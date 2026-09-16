import assert from "node:assert/strict";
import test from "node:test";

import { createDashboardView } from "../../docs/dashboard-view.mjs";
import { FakeDocument } from "./fixtures/fake-document.mjs";
import { FakeNode } from "./fixtures/fake-node.mjs";
import { findNode } from "./fixtures/helpers.mjs";

test("blocked delivery exposes a retry action for its run", () => {
  const dashboardOutput = new FakeNode("section");
  const actionPayloads = [];
  const view = createDashboardView({
    document: new FakeDocument(),
    summaryOutput: new FakeNode("section"),
    dashboardOutput,
    actionLog: new FakeNode("section"),
    actionsOutput: new FakeNode("div"),
    runAction: (payload) => actionPayloads.push(payload),
  });
  view.render({
    counts: {},
    repositories: [{
      name: "owner/api",
      active: true,
      writer: { status: "idle" },
      pbis: [{
        number: 40,
        title: "Commit and push",
        status: "completed",
        run_id: "run-40",
        delivery: {
          status: "push_failed",
          commit_sha: "abc123",
          evidence: "Local commit preserved.",
          retry_available: true,
        },
      }],
    }],
  });

  const retry = findNode(
    dashboardOutput,
    (node) => node.tag === "button" && node.textContent === "Retry commit and push",
  );
  assert.ok(retry);
  retry.click();
  assert.deepEqual(actionPayloads, [{
    action: "commit_push",
    repository: "owner/api",
    pbi_number: 40,
    run_id: "run-40",
  }]);
});

test("dependency readiness explains empty and unknown evidence", () => {
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
    counts: {},
    repositories: [{
      name: "owner/api",
      active: true,
      writer: { status: "idle" },
      pbis: [
        {
          number: 1,
          title: "Empty Epic",
          status: "idle",
          issue_state: "OPEN",
          state_reason: "REOPENED",
          subtasks: [],
          dependency_readiness: {
            status: "unknown",
            counts: {
              ready: 0,
              incomplete: 0,
              blocked: 0,
              rejected: 0,
              completed: 0,
              unknown: 0,
            },
            reasons: ["no_linked_children"],
            observed_at: "2026-09-14T08:00:00+00:00",
          },
        },
        {
          number: 2,
          title: "Unknown child facts",
          status: "idle",
          subtasks: [{
            id: "#3",
            title: "Unobserved child",
            readiness: "unknown",
            issue_state: "OPEN",
            state_reason: "REOPENED",
            project_status: null,
            dependency_read_complete: false,
            dependency_read_error: "permission_denied",
            readiness_reasons: ["permission_denied"],
            observed_at: "2026-09-14T08:00:00+00:00",
          }],
          dependency_readiness: {
            status: "unknown",
            counts: {
              ready: 0,
              incomplete: 0,
              blocked: 0,
              rejected: 0,
              completed: 0,
              unknown: 1,
            },
            reasons: ["child_evidence_unknown"],
          },
        },
        {
          number: 4,
          title: "Missing readiness payload",
          status: "idle",
          subtasks: [{ id: "#5", title: "Child" }],
        },
      ],
    }],
  });

  assert.match(dashboardOutput.textContent, /Issue OPEN \(REOPENED\)/);
  assert.match(dashboardOutput.textContent, /Parent readiness: unknown/);
  assert.match(dashboardOutput.textContent, /no_linked_children/);
  assert.match(dashboardOutput.textContent, /Readiness: unknown/);
  assert.match(dashboardOutput.textContent, /Blocked-by facts unavailable \(permission_denied\)/);
  assert.match(dashboardOutput.textContent, /Observed: 2026-09-14T08:00:00\+00:00/);
  assert.match(dashboardOutput.textContent, /Unknown\. Readiness evidence is unavailable\./);
});

test("active and failed PBIs expose run controls through the dashboard action boundary", () => {
  const dashboardOutput = new FakeNode("section");
  const actionLog = new FakeNode("section");
  const actionsOutput = new FakeNode("div");
  const actionPayloads = [];
  const view = createDashboardView({
    document: new FakeDocument(),
    summaryOutput: new FakeNode("section"),
    dashboardOutput,
    actionLog,
    actionsOutput,
    runAction: (payload) => actionPayloads.push(payload),
  });

  view.render({
    counts: {},
    repositories: [{
      name: "owner/api",
      active: true,
      writer: { status: "active" },
      pbis: [
        {
          number: 1,
          title: "Advance me",
          status: "active",
          stage: "refine",
          run_id: "run-1",
        },
        {
          number: 2,
          title: "Retry me",
          status: "failed",
          claimable: true,
          run_id: "run-2",
        },
      ],
    }],
    actions: [{
      kind: "advance",
      status: "succeeded",
      result: { run: { stage: "implement" } },
    }],
  });

  const advance = findNode(
    dashboardOutput,
    (node) => node.tag === "button" && node.textContent === "Advance to implementation",
  );
  const retry = findNode(
    dashboardOutput,
    (node) => node.tag === "button" && node.textContent === "Retry writer",
  );
  assert.ok(advance);
  assert.ok(retry);
  advance.click();
  retry.click();
  assert.deepEqual(actionPayloads, [
    {
      action: "advance",
      repository: "owner/api",
      pbi_number: 1,
      run_id: "run-1",
      target: "implement",
    },
    {
      action: "retry",
      repository: "owner/api",
      pbi_number: 2,
      run_id: "run-2",
    },
  ]);
  assert.match(actionsOutput.textContent, /Result:.*implement/);
});

test("awaiting operator PBIs expose the stop control", () => {
  const dashboardOutput = new FakeNode("section");
  const actionPayloads = [];
  const view = createDashboardView({
    document: new FakeDocument(),
    summaryOutput: new FakeNode("section"),
    dashboardOutput,
    actionLog: new FakeNode("section"),
    actionsOutput: new FakeNode("div"),
    runAction: (payload) => actionPayloads.push(payload),
  });

  view.render({
    counts: {},
    repositories: [{
      name: "owner/api",
      active: true,
      writer: { status: "awaiting_operator" },
      pbis: [{
        number: 1,
        title: "Needs an answer",
        status: "awaiting_operator",
        run_id: "run-1",
      }],
    }],
  });

  const stop = findNode(
    dashboardOutput,
    (node) => node.tag === "button" && node.textContent === "Stop",
  );
  assert.ok(stop);
  stop.click();
  assert.deepEqual(actionPayloads, [{
    action: "stop",
    repository: "owner/api",
    run_id: "run-1",
  }]);
});

test("action errors stay bounded in the browser projection", () => {
  const actionLog = new FakeNode("section");
  const actionsOutput = new FakeNode("div");
  const view = createDashboardView({
    document: new FakeDocument(),
    summaryOutput: new FakeNode("section"),
    dashboardOutput: new FakeNode("section"),
    actionLog,
    actionsOutput,
    runAction: () => {},
  });

  view.render({ actions: [{ kind: "start", status: "failed", error: "x".repeat(5_000) }] });

  assert.equal(actionsOutput.textContent.includes("x".repeat(4_001)), false);
});

test("inactive PBIs expose evidence without mutation controls", () => {
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
    counts: {},
    repositories: [{
      name: "owner/api",
      active: true,
      writer: { status: "active" },
      pbis: [{
        number: 1,
        title: "Removed PBI",
        active: false,
        status: "active",
        run_id: "run-1",
        delivery: { status: "retained", retry_available: true },
      }],
    }],
  });

  assert.equal(
    dashboardOutput.children.some((node) => node.tag === "button"),
    false,
  );
});
