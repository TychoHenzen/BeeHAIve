import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("building signal page exposes bounded lifecycle and persisted readback", async () => {
  const page = await readFile("docs/building-signal.html", "utf8");

  assert.match(page, /maxlength="4000"/);
  assert.match(page, /id="generate"/);
  assert.match(page, /id="update"/);
  assert.match(page, /id="confirm"/);
  assert.match(page, /id="assign"/);
  assert.match(page, /id="evaluate"/);
  assert.match(page, /id="reload">Reload readback/);
  assert.match(page, /building-signals/);
  assert.ok(page.includes("/generate"));
  assert.ok(page.includes("/confirm"));
  assert.ok(page.includes("/assign"));
  assert.ok(page.includes("/evaluate"));
  assert.match(page, /method: "PUT"/);
  assert.match(page, /history\.replaceState/);
  assert.match(page, /Persisted building signal state reloaded/);
  assert.doesNotMatch(page, /localStorage|sessionStorage/);
});
