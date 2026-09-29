import assert from "node:assert/strict";
import test from "node:test";

import { interpolatePosition, pointFor, regionSize } from "../beehaiive/web/hive.js";

const region = {
  workflow_id: 1,
  revision: 2,
  stations: [
    { id: "wait", layout: { x: 0, y: 0 } },
    { id: "run", layout: { x: 280, y: 150 } },
  ],
};

test("Hive layout preserves workflow coordinates and interpolates movement", () => {
  assert.deepEqual(pointFor(region, "run", 40), { x: 390, y: 236 });
  assert.deepEqual(regionSize(region), { width: 780, height: 270 });
  assert.deepEqual(
    interpolatePosition({ x: 0, y: 10 }, { x: 100, y: 50 }, 0.25),
    { x: 25, y: 20 },
  );
  assert.deepEqual(
    interpolatePosition({ x: 0, y: 10 }, { x: 100, y: 50 }, 2),
    { x: 100, y: 50 },
  );
});
