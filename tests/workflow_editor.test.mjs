import assert from "node:assert/strict";
import test from "node:test";

import { settle } from "./dom/helpers.mjs";
import {
  createHarness,
  findText,
  invalidValidation,
  validPayload,
} from "./dom/workflow_harness.mjs";

test("workflow editor drives example, drag, duplicate, edge, and validation-pin behavior", async () => {
  const harness = await createHarness();
  const { document } = harness;
  document.querySelector("#workflow-example").click();
  await settle();

  const canvas = document.querySelector("#workflow-canvas");
  assert.equal(canvas.querySelectorAll("[data-state-id]").length, 8);
  assert.equal(canvas.querySelectorAll("[data-transition-index]").length, 15);

  const wait = canvas.querySelector("[data-state-id=\"wait\"]");
  wait.dispatch("pointerdown", { pointerId: 1, clientX: 10, clientY: 10 });
  wait.dispatch("pointermove", { pointerId: 1, clientX: 90, clientY: 50 });
  wait.dispatch("pointerup", { pointerId: 1, clientX: 90, clientY: 50 });
  await settle();
  const dragged = harness.validationPayloads.at(-1).definition.states.find((state) => state.id === "wait");
  assert.deepEqual(dragged.layout, { x: 80, y: 210 });

  let duplicate = findText(canvas.querySelector("[data-state-id=\"wait\"]"), "Duplicate");
  duplicate.click();
  duplicate = findText(
    document.querySelector("#workflow-canvas").querySelector("[data-state-id=\"wait\"]"),
    "Duplicate",
  );
  duplicate.click();
  const ids = document
    .querySelector("#workflow-canvas")
    .querySelectorAll("[data-state-id]")
    .map((state) => state.dataset.stateId);
  assert.equal(new Set(ids).size, ids.length);

  document.querySelector("#workflow-add-transition").click();
  await settle();
  assert.equal(document.querySelector("#workflow-canvas").querySelectorAll("[data-transition-index]").length, 16);
  const added = harness.validationPayloads.at(-1).definition.transitions.at(-1);
  assert.equal(added.conditions[0].kind, "item_status_is");

  harness.setInvalid(true);
  document.querySelector("#workflow-add-parameter").click();
  await settle();
  assert.equal(document.querySelector("#workflow-save").disabled, true);
  assert.equal(document.querySelector("[data-state-id=\"wait\"]").attributes["aria-invalid"], "true");
  assert.equal(document.querySelector("[data-parameter-name=\"backlog\"]").attributes["aria-invalid"], "true");
  assert.equal(document.querySelector("[data-transition-index=\"0\"]").attributes["aria-invalid"], "true");
});

test("workflow editor ignores stale validation responses", async () => {
  const harness = await createHarness({ deferredValidation: true });
  const { document, validationResolvers } = harness;
  document.querySelector("#workflow-example").click();
  harness.resolveValidation(validPayload());
  await settle();

  const firstTitle = document.querySelector("[aria-label=\"wait title\"]");
  firstTitle.value = "older";
  firstTitle.dispatch("change");
  const secondTitle = document.querySelector("[aria-label=\"wait title\"]");
  secondTitle.value = "newer";
  secondTitle.dispatch("change");
  assert.equal(validationResolvers.length, 2);
  harness.resolveValidation(invalidValidation());
  harness.resolveValidation(validPayload());
  await settle();

  assert.equal(document.querySelector("#workflow-save").disabled, false);
  assert.equal(document.querySelector("#workflow-errors").textContent, "Definition is valid.");
});
