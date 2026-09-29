import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const app = await readFile(new URL("../../beehaiive/core/web/app.js", import.meta.url), "utf8");
const styles = await readFile(new URL("../../beehaiive/core/web/styles.css", import.meta.url), "utf8");
const index = await readFile(new URL("../../beehaiive/core/web/index.html", import.meta.url), "utf8");

test("workflow UI exposes the live node editor contract", () => {
  assert.match(app, /function exampleWorkflow\(\)/);
  for (const skill of [
    "refine-backlog-item",
    "next-ticket",
    "submit-draft-pr",
    "review-pr",
    "fix-pr-review",
    "complete-pr",
  ]) {
    assert.match(app, new RegExp(skill));
  }
  assert.match(app, /pointerdown/);
  assert.match(app, /state\.layout =/);
  assert.match(app, /createElementNS/);
  assert.match(app, /workflow-edges/);
  assert.match(app, /dataset\.stateIndex/);
  assert.match(app, /dataset\.transitionIndex/);
  assert.match(app, /conditionKinds/);
  assert.match(app, /markValidationErrors/);
  assert.match(app, /transitionConditionFor/);
  assert.match(app, /workflow-example.*exampleWorkflow/s);
  assert.match(styles, /\.state-card\s*\{[\s\S]*position:\s*absolute/);
  assert.match(index, /role="application" aria-label="Workflow node canvas"/);
});
