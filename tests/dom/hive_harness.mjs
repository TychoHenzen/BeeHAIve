import { FakeDocument } from "./fake_document.mjs";
import { response, settle } from "./helpers.mjs";
import { activeAlert, snapshot } from "./hive_snapshot.mjs";

const HIVE_IDS = [
  "hive-map",
  "hive-details",
  "hive-status",
  "hive-alert-list",
  "hive-refresh",
  "hive-motion",
];
const GLOBAL_NAMES = [
  "document",
  "window",
  "fetch",
  "setTimeout",
  "clearTimeout",
  "requestAnimationFrame",
];
let importCount = 0;

function acknowledge(env, options) {
  env.acknowledgementBody = options.body;
  env.currentSnapshot = snapshot([]);
  return response({ pass_id: "pass-1", acknowledged_by: "operator" });
}

function nextEvents(env) {
  env.eventRequestCount += 1;
  const first = env.eventRequestCount === 1;
  return response({
    cursor: `cursor-${env.eventRequestCount}`,
    events: first ? [{ event: "step_finished", pass_id: "pass-1" }] : [],
  });
}

function createFetch(env) {
  return async (path, options = {}) => {
    env.fetchCalls.push({ path, options });
    if (path === "/api/hive") return response(env.currentSnapshot);
    if (path.startsWith("/api/hive/events")) return nextEvents(env);
    if (path.startsWith("/api/agents/agent-1/logs")) return response({ lines: ["log line"] });
    if (path === "/api/passes/pass-1/acknowledge") return acknowledge(env, options);
    throw new Error(`unexpected fetch ${path}`);
  };
}

function installGlobals(env) {
  globalThis.document = env.document;
  globalThis.window = { matchMedia: () => ({ matches: env.reducedMotion }) };
  globalThis.fetch = createFetch(env);
  globalThis.setTimeout = (callback, delay) => {
    env.scheduled.push({ callback, delay });
    return env.scheduled.length;
  };
  globalThis.clearTimeout = () => {};
  globalThis.requestAnimationFrame = (callback) => {
    env.frames.push(callback);
    return env.frames.length;
  };
}

function restoreGlobals(previous) {
  for (const [key, value] of Object.entries(previous)) {
    if (value === undefined) delete globalThis[key];
    else globalThis[key] = value;
  }
}

export async function startHive(t, { alerts = [activeAlert()] } = {}) {
  const env = {
    document: new FakeDocument(HIVE_IDS),
    currentSnapshot: snapshot(alerts),
    reducedMotion: true,
    eventRequestCount: 0,
    acknowledgementBody: null,
    fetchCalls: [],
    scheduled: [],
    frames: [],
  };
  const previous = Object.fromEntries(GLOBAL_NAMES.map((name) => [name, globalThis[name]]));
  t.after(() => restoreGlobals(previous));
  installGlobals(env);
  importCount += 1;
  const url = `../../beehaiive/web/hive.js?behavior=${Date.now()}-${importCount}`;
  env.hive = await import(url);
  await settle();
  await settle();
  return env;
}
