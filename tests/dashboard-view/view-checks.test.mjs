import assert from "node:assert/strict";
import test from "node:test";

import { createDashboardView } from "../../docs/dashboard-view.mjs";
import { FakeDocument } from "./fixtures/fake-document.mjs";
import { FakeNode } from "./fixtures/fake-node.mjs";
import { findNode } from "./fixtures/helpers.mjs";

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
