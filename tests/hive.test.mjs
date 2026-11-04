import assert from "node:assert/strict";
import test from "node:test";

import { settle } from "./dom/helpers.mjs";
import { startHive } from "./dom/hive_harness.mjs";

test("Hive geometry helpers place stations and interpolate positions", async (t) => {
  const { currentSnapshot, hive } = await startHive(t);

  assert.deepEqual(hive.pointFor(currentSnapshot.regions[0], "run", 40), { x: 390, y: 86 });
  assert.deepEqual(hive.regionSize(currentSnapshot.regions[0]), { width: 780, height: 160 });
  assert.deepEqual(
    hive.interpolatePosition({ x: 0, y: 10 }, { x: 100, y: 50 }, 0.25),
    { x: 25, y: 20 },
  );
});

test("Hive map zooms and pans the viewport", async (t) => {
  const { document } = await startHive(t);

  const map = document.querySelector("#hive-map");
  const svg = map.querySelector("svg");
  const initialViewBox = svg.attributes.viewBox;
  map.querySelector('[aria-label="Zoom in"]').click();
  assert.notEqual(svg.attributes.viewBox, initialViewBox);
  svg.dispatch("pointerdown", { button: 0, pointerId: 1, clientX: 100, clientY: 100 });
  svg.dispatch("pointermove", { pointerId: 1, clientX: 60, clientY: 80 });
  svg.dispatch("pointerup", { pointerId: 1 });
  assert.notEqual(svg.attributes.viewBox, initialViewBox);
});

test("Hive polls events every two seconds and skips animation under reduced motion", async (t) => {
  const { fetchCalls, frames, scheduled } = await startHive(t);

  const scheduledPoll = scheduled.shift();
  assert.equal(scheduledPoll.delay, 2000);
  await scheduledPoll.callback();
  await settle();
  assert.ok(fetchCalls.some(({ path }) => path.startsWith("/api/hive/events?since=")));
  assert.equal(scheduled.at(-1).delay, 2000);
  assert.equal(frames.length, 0);
});

test("Hive motion button forces reduced motion off and starts animation frames", async (t) => {
  const env = await startHive(t);

  const motionButton = env.document.querySelector("#hive-motion");
  motionButton.click();
  await settle();
  env.reducedMotion = false;
  motionButton.click();
  await settle();
  assert.ok(env.frames.length > 0);
});

test("Hive agent click shows the agent details and its log lines", async (t) => {
  const { document } = await startHive(t);

  document.querySelector("#hive-map").querySelector('[data-agent-id="agent-1"]').click();
  await settle();
  await settle();
  assert.match(document.querySelector("#hive-details").textContent, /worker/);
  assert.match(document.querySelector("#hive-details").textContent, /log line/);
});

test("Hive station click shows the station title and skill", async (t) => {
  const { document } = await startHive(t);

  document.querySelector("#hive-map").querySelector('[data-station-id="run"]').click();
  assert.match(document.querySelector("#hive-details").textContent, /Run skill/);
  assert.match(document.querySelector("#hive-details").textContent, /deliver/);
});

test("Hive alert acknowledgement posts the operator and clears the list", async (t) => {
  const env = await startHive(t);
  const { document } = env;

  document.querySelector("#hive-alert-list").querySelector("button").click();
  await settle();
  await settle();
  assert.equal(env.acknowledgementBody, JSON.stringify({ acknowledged_by: "operator" }));
  assert.equal(document.querySelector("#hive-alert-list").textContent, "No active alerts.");
});
