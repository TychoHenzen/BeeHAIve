import assert from "node:assert/strict";
import test from "node:test";

import { createDashboardClient } from "../docs/dashboard-client.mjs";

function response(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  };
}

function harness(fetcher) {
  const states = [];
  const statuses = [];
  const busy = [];
  const client = createDashboardClient({
    fetcher,
    projectId: () => "owner:7",
    apiKey: () => "test-key",
    onState: (state) => states.push(state),
    onStatus: (message, kind = "") => statuses.push({ message, kind }),
    onBusy: (value) => busy.push(value),
  });
  return { client, states, statuses, busy };
}

test("refresh ignores an older response that arrives after a newer one", async () => {
  let calls = 0;
  const fetcher = () => {
    calls += 1;
    const payload = { updated_at: calls === 1 ? "old" : "new" };
    const delay = calls === 1 ? 20 : 0;
    return new Promise((resolve) => {
      setTimeout(() => resolve(response(payload)), delay);
    });
  };
  const { client, states, statuses } = harness(fetcher);

  await Promise.all([client.refresh(), client.refresh()]);

  assert.deepEqual(states, [{ updated_at: "new" }]);
  assert.equal(statuses.at(-1).kind, "success");
});

test("refresh reports a failure without clearing the previous state", async () => {
  let calls = 0;
  const fetcher = async () => {
    calls += 1;
    return calls === 1
      ? response({ updated_at: "known" })
      : response({ detail: "fixture unavailable" }, 502);
  };
  const { client, states, statuses } = harness(fetcher);

  await client.refresh();
  await client.refresh();

  assert.deepEqual(states, [{ updated_at: "known" }]);
  assert.equal(statuses.at(-1).kind, "failure");
  assert.match(statuses.at(-1).message, /fixture unavailable/);
});

test("actions expose pending, success, and failure states", async () => {
  let resolveAction;
  const fetcher = () => new Promise((resolve) => {
    resolveAction = resolve;
  });
  const { client, statuses, busy } = harness(fetcher);

  const success = client.runAction({ action: "approve" });
  assert.equal(statuses.at(-1).kind, "pending");
  resolveAction(response({ action: { status: "succeeded" }, state: { version: 1 } }));
  await success;
  assert.equal(statuses.at(-1).kind, "success");
  assert.deepEqual(busy, [true, false]);

  const failed = harness(async () => (
    response({ action: { status: "failed", error: "rejected" }, state: { version: 2 } })
  ));
  await failed.client.runAction({ action: "clarify" });
  assert.equal(failed.statuses.at(-1).kind, "failure");
  assert.match(failed.statuses.at(-1).message, /rejected/);
});
