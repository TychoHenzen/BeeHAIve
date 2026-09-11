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

test("rendering exposes current-head check verdicts and evidence", () => {
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
            number: 1,
            title: "Checked PBI",
            stage_progress: [],
            pull_requests: [{ number: 9, state: "open" }],
            checks: {
              verdict: "blocking",
              pull_requests: [
                {
                  number: 9,
                  head_sha: "abc123",
                  verdict: "blocking",
                  contexts: [
                    {
                      name: "codeql",
                      verdict: "blocking",
                      required: true,
                      state: "failure",
                      url: "https://example.test/codeql",
                    },
                    {
                      name: "legacy-status",
                      verdict: "passing",
                      required: false,
                      state: "success",
                    },
                  ],
                },
              ],
            },
            subtasks: [],
            readers: [],
            reviewers: {},
            activity: [],
          },
        ],
      },
    ],
  });

  assert.match(dashboardOutput.textContent, /Checks/);
  assert.match(dashboardOutput.textContent, /Overall: blocking/);
  assert.match(dashboardOutput.textContent, /PR #9: blocking; head abc123/);
  assert.match(dashboardOutput.textContent, /codeql; blocking; required; failure/);
  assert.match(dashboardOutput.textContent, /https:\/\/example.test\/codeql/);
  assert.match(dashboardOutput.textContent, /legacy-status; passing; optional; success/);

  const blocking = findNode(
    dashboardOutput,
    (node) => node._textContent === "Overall: blocking",
  );
  assert.equal(blocking.className, "status failure");
});

test("rendering archived PBIs exposes completion and branch evidence", () => {
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
    counts: { pbis: 1 },
    repositories: [
      {
        name: "owner/api",
        active: true,
        writer: { status: "idle" },
        pbis: [
          {
            number: 1,
            title: "Archived PBI",
            archived: true,
            source_url: "https://example.test/issues/1",
            pull_request_url: "https://example.test/pull/9",
            pull_requests: [
              {
                number: 9,
                merged: true,
                url: "https://example.test/pull/9",
                source_branch: "codex/done",
                source_branch_state: "deleted",
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

  assert.match(dashboardOutput.textContent, /Archived: completion evidence verified/);
  assert.match(dashboardOutput.textContent, /Source issue: https:\/\/example.test\/issues\/1/);
  assert.match(dashboardOutput.textContent, /branch codex\/done \(deleted\)/);

  const links = [];
  findNode(dashboardOutput, (node) => {
    if (node.tag === "a") links.push(node);
    return false;
  });
  assert.equal(links.length, 2);
  assert.equal(links[0].target, "_blank");
  assert.equal(links[0].rel, "noopener noreferrer");
  assert.equal(links[0].href, "https://example.test/issues/1");
  assert.equal(links[1].href, "https://example.test/pull/9");
});
