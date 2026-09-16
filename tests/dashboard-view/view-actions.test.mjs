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

test("graph state exposes draft controls and version readback", () => {
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
    graph: {
      workflow_id: "dashboard-flow",
      active: { revision: 1 },
      definitions: [{
        workflow_id: "dashboard-flow",
        revision: 1,
        nodes: [{ node_id: "start", kind: "prompt" }],
        edges: Array.from({ length: 128 }, (_, index) => ({
          source: "start",
          target: "done",
          condition: `condition-${index}`,
        })),
        definition_hash: "abc",
        safety_evidence: {
          evidence_hash: "def",
          evidence: {
            safety: {
              passed: false,
              checks: [{ name: "reachability", status: "fail", reason: "unused node" }],
            },
          },
        },
        review: { actor: "operator" },
        active: true,
      }],
    },
    repositories: [],
  });

  assert.match(dashboardOutput.textContent, /Workflow graph/);
  assert.match(dashboardOutput.textContent, /Nodes: start \(prompt\)/);
  assert.match(dashboardOutput.textContent, /condition-127/);
  assert.match(dashboardOutput.textContent, /reachability: fail/);
  const evaluate = findNode(
    dashboardOutput,
    (node) => node.tag === "button" && node.textContent === "Evaluate draft",
  );
  assert.ok(evaluate);
  evaluate.click();
  assert.equal(actionPayloads[0].action, "graph_evaluate");
  assert.equal(actionPayloads[0].workflow_id, "dashboard-flow");
  assert.equal(actionPayloads[0].candidate.workflow_id, "dashboard-flow");
});

test("graph revision controls include baseline data for activation", () => {
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
    graph: {
      workflow_id: "dashboard-flow",
      active: { revision: 1 },
      definitions: [
        { workflow_id: "dashboard-flow", revision: 1, nodes: [], edges: [] },
        { workflow_id: "dashboard-flow", revision: 2, nodes: [], edges: [] },
      ],
    },
    repositories: [],
  });

  const activate = findNode(
    dashboardOutput,
    (node) => node.tag === "button" && node.textContent === "Activate reviewed draft",
  );
  assert.ok(activate);
  activate.click();
  assert.equal(actionPayloads[0].action, "graph_activate");
  assert.equal(actionPayloads[0].baseline.revision, 1);
  assert.deepEqual(actionPayloads[0].baseline_fixtures, {});
});

test("graph drafts survive a polling rerender", () => {
  const dashboardOutput = new FakeNode("section");
  const view = createDashboardView({
    document: new FakeDocument(),
    summaryOutput: new FakeNode("section"),
    dashboardOutput,
    actionLog: new FakeNode("section"),
    actionsOutput: new FakeNode("div"),
    runAction: () => {},
  });
  const state = {
    counts: {},
    graph: {
      workflow_id: "dashboard-flow",
      active: null,
      definitions: [],
    },
    repositories: [],
  };

  view.render(state);
  const draft = findNode(dashboardOutput, (node) => node.tag === "textarea");
  assert.ok(draft);
  draft.value = '{"workflow_id":"dashboard-flow","revision":1}';
  draft.listeners.get("input")();
  view.render(state);
  const rerendered = findNode(dashboardOutput, (node) => node.tag === "textarea");
  assert.ok(rerendered);
  assert.equal(rerendered.value, draft.value);
});

test("graph drafts do not cross project boundaries", () => {
  const dashboardOutput = new FakeNode("section");
  const view = createDashboardView({
    document: new FakeDocument(),
    summaryOutput: new FakeNode("section"),
    dashboardOutput,
    actionLog: new FakeNode("section"),
    actionsOutput: new FakeNode("div"),
    runAction: () => {},
  });
  const base = {
    counts: {},
    graph: { workflow_id: "dashboard-flow", active: null, definitions: [] },
    repositories: [],
  };

  view.render({ ...base, project_id: "project-a" });
  const firstDraft = findNode(dashboardOutput, (node) => node.tag === "textarea");
  assert.ok(firstDraft);
  firstDraft.value = '{"workflow_id":"dashboard-flow","revision":99}';
  firstDraft.listeners.get("input")();
  view.render({ ...base, project_id: "project-b" });
  const secondDraft = findNode(dashboardOutput, (node) => node.tag === "textarea");
  assert.ok(secondDraft);
  assert.notEqual(secondDraft.value, firstDraft.value);
});
