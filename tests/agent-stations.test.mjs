import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("agent station page exposes native SVG, bounded issue controls, and polling route", async () => {
  const html = await readFile("docs/agent-stations.html", "utf8");
  const script = await readFile("docs/agent-stations.js", "utf8");
  const dashboard = await readFile("docs/dashboard.html", "utf8");
  assert.match(html, /station-grid/);
  assert.match(html, /agent-stations\.js/);
  assert.match(script, /agent-stations/);
  assert.match(script, /issue-actions/);
  assert.match(script, /createElementNS/);
  assert.match(script, /textContent/);
  assert.match(dashboard, /agent-stations/);
  assert.doesNotMatch(script, /innerHTML/);
});
