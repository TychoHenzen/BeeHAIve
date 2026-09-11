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

function harness(fetcher, options = {}) {
  const states = [];
  const statuses = [];
  const busy = [];
  const client = createDashboardClient({
    fetcher,
    projectId: options.projectId || (() => "owner:7"),
    archived: options.archived,
    apiKey: () => "test-key",
    saveApiKey: options.saveApiKey,
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

test("refresh requests the archived dashboard view when enabled", async () => {
  let requestedUrl;
  const { client } = harness(async (url) => {
    requestedUrl = url;
    return response({ updated_at: "archived" });
  }, { archived: () => true });

  await client.refresh();

  assert.equal(requestedUrl, "/projects/owner%3A7/dashboard?archived=true");
});

test("actions preserve the archived dashboard view when enabled", async () => {
  let requestedUrl;
  const { client } = harness(async (url) => {
    requestedUrl = url;
    return response({ action: { status: "succeeded" }, state: { archived: true } });
  }, { archived: () => true });

  await client.runAction({ action: "approve" });

  assert.equal(requestedUrl, "/projects/owner%3A7/actions?archived=true");
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

test("clearing the project invalidates an in-flight refresh", async () => {
  let currentProject = "owner:7";
  let resolveRequest;
  const fetcher = () => new Promise((resolve) => {
    resolveRequest = resolve;
  });
  const { client, states, statuses } = harness(fetcher, {
    projectId: () => currentProject,
  });

  const pendingRefresh = client.refresh();
  currentProject = "";
  await client.refresh();
  resolveRequest(response({ updated_at: "stale" }));
  await pendingRefresh;

  assert.deepEqual(states, []);
  assert.equal(statuses.at(-1).message, "Enter a project ID to load live state.");
});

test("an action invalidates an older refresh response", async () => {
  let resolveRefresh;
  let resolveAction;
  const fetcher = (url) => new Promise((resolve) => {
    if (url.endsWith("/dashboard")) resolveRefresh = resolve;
    else resolveAction = resolve;
  });
  const { client, states, statuses } = harness(fetcher);

  const pendingRefresh = client.refresh();
  const action = client.runAction({ action: "approve" });
  resolveAction(response({ action: { status: "succeeded" }, state: { version: 2 } }));
  await action;
  resolveRefresh(response({ updated_at: "stale" }));
  await pendingRefresh;

  assert.deepEqual(states, [{ version: 2 }]);
  assert.equal(statuses.at(-1).kind, "success");
});

test("storage errors still clear action busy state", async () => {
  const { client, busy, statuses } = harness(
    async () => response({ action: { status: "succeeded" } }),
    { saveApiKey: () => { throw new Error("storage unavailable"); } },
  );

  await client.runAction({ action: "approve" });

  assert.deepEqual(busy, [true, false]);
  assert.equal(statuses.at(-1).kind, "failure");
  assert.match(statuses.at(-1).message, /storage unavailable/);
});
