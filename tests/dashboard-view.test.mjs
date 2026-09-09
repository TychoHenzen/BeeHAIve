import assert from "node:assert/strict";
import test from "node:test";

import { createDashboardView } from "../docs/dashboard-view.mjs";

class FakeNode {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.listeners = new Map();
    this._textContent = "";
    this.hidden = false;
  }

  set textContent(value) {
    this._textContent = String(value);
  }

  get textContent() {
    return this._textContent + this.children.map((child) => child.textContent).join("");
  }

  append(...nodes) {
    this.children.push(...nodes);
  }

  replaceChildren(...nodes) {
    this.children = nodes;
  }

  addEventListener(type, listener) {
    this.listeners.set(type, listener);
  }

  click() {
    this.listeners.get("click")?.();
  }
}

class FakeDocument {
  createElement(tag) {
    return new FakeNode(tag);
  }
}

function findNode(node, predicate) {
  if (predicate(node)) return node;
  for (const child of node.children) {
    const result = findNode(child, predicate);
    if (result) return result;
  }
  return null;
}

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
            subtasks: [{ id: "#2", title: "Test API" }],
            escalation: { current: 0, current_tier: "terra", consecutive: 0 },
            escalation_log: [{ tier: "terra", resolved: false }],
            readers: [],
            reviewers: {},
            activity: [],
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
  assert.match(dashboardOutput.textContent, /Live API/);
  assert.match(dashboardOutput.textContent, /Pull request/);
  assert.match(dashboardOutput.textContent, /Tier terra/);
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
