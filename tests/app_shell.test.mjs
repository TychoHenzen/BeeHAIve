import assert from "node:assert/strict";
import test from "node:test";

import { settle } from "./dom/helpers.mjs";
import { createHarness } from "./dom/workflow_harness.mjs";

const RUN_STEP = {
  sequence: 1,
  state_id: "run",
  status: "completed",
  outcome: "done",
  summary: "finished",
};

function runnerAgent() {
  return {
    id: "agent-1",
    name: "runner",
    status: "stopped",
    workflow_id: 7,
    workflow_revision: 1,
    repository: "owner/repo",
    checkout_path: "C:/agents/runner",
    current_state: null,
    history: [{ id: "pass-1", status: "completed", item: { title: "PBI" }, steps: [RUN_STEP] }],
    alerts: [{ kind: "escalated", message: "needs operator" }],
  };
}

function deliveryWorkflow() {
  const definition = {
    parameters: [
      { name: "skill", type: "skill", const: false, value: null },
      { name: "status", type: "status", const: true, value: "Todo" },
    ],
  };
  return { id: 7, name: "Delivery", detail: { id: 7, latest: { definition } } };
}

test("core tabs activate the Hive panel", async () => {
  const { document } = await createHarness();
  const hiveTab = document.querySelector('[data-tab="hive"]');
  const hivePanel = document.querySelector('[data-panel="hive"]');
  hiveTab.click();
  assert.ok(hiveTab.className.split(/\s+/).includes("active"));
  assert.ok(hivePanel.className.split(/\s+/).includes("active"));
  assert.ok(!document.querySelector('[data-tab="board"]').className.split(/\s+/).includes("active"));
  assert.ok(!document.querySelector('[data-panel="board"]').className.split(/\s+/).includes("active"));
});

test("agents view exposes assignment form and durable run evidence", async () => {
  const harness = await createHarness({
    workflows: [deliveryWorkflow()],
    agents: [runnerAgent()],
  });
  const { document } = harness;
  document.querySelector("#agents-refresh").click();
  await settle();

  assert.match(document.querySelector("#agent-list").textContent, /runner/);
  assert.match(document.querySelector("#agent-list").textContent, /run/);
  assert.match(document.querySelector("#agent-list").textContent, /needs operator/);

  document.querySelector("#agent-new").click();
  await settle();
  assert.equal(document.querySelector("#agent-form").hidden, false);
  assert.equal(document.querySelector("#agent-parameters").querySelectorAll("[data-agent-parameter-name]").length, 1);
  assert.match(document.querySelector("#agent-parameters").textContent, /constant: Todo/);
});
