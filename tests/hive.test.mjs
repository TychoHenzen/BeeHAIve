import assert from "node:assert/strict";
import test from "node:test";

function camelCase(attribute) {
  return attribute.replace(/^data-/, "").replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
}

function selectorMatches(node, selector) {
  if (selector.startsWith("#")) return node.id === selector.slice(1);
  if (selector.startsWith(".")) return node.className.split(/\s+/).includes(selector.slice(1));
  const attribute = selector.match(/^\[([^=\]]+)(?:=["']([^"']*)["'])?\]$/);
  if (attribute) {
    const [, name, expected] = attribute;
    const value = name.startsWith("data-") ? node.dataset[camelCase(name)] : node.attributes[name];
    return expected === undefined ? value !== undefined : String(value) === expected;
  }
  return node.tagName.toLowerCase() === selector.toLowerCase();
}

class FakeNode {
  constructor(tagName, id = "") {
    this.tagName = tagName.toUpperCase();
    this.id = id;
    this.children = [];
    this.listeners = new Map();
    this.attributes = {};
    this.dataset = {};
    this.style = {};
    this.className = "";
    this.hidden = false;
    this.value = "";
    this._text = "";
  }

  set textContent(value) {
    this._text = String(value);
    this.children = [];
  }

  get textContent() {
    return this._text + this.children.map((child) => child.textContent).join("");
  }

  get classList() {
    return {
      add: (...names) => {
        this.className = [...new Set(`${this.className} ${names.join(" ")}`.trim().split(/\s+/))].join(" ");
      },
      remove: (...names) => {
        this.className = this.className.split(/\s+/).filter((name) => !names.includes(name)).join(" ");
      },
    };
  }

  append(...nodes) {
    this.children.push(...nodes);
  }

  replaceChildren(...nodes) {
    this.children = nodes;
    this._text = "";
  }

  addEventListener(type, listener) {
    this.listeners.set(type, listener);
  }

  dispatch(type, event = {}) {
    const dispatched = {
      ...event,
      target: this,
      currentTarget: this,
      defaultPrevented: false,
      preventDefault() {
        dispatched.defaultPrevented = true;
      },
    };
    const listener = this.listeners.get(type);
    if (listener) listener(dispatched);
    return dispatched;
  }

  click() {
    return this.dispatch("click");
  }

  setAttribute(attribute, value) {
    this.attributes[attribute] = String(value);
    if (attribute.startsWith("data-")) this.dataset[camelCase(attribute)] = String(value);
  }

  querySelectorAll(selector) {
    const matches = [];
    const visit = (node) => {
      for (const child of node.children) {
        if (selectorMatches(child, selector)) matches.push(child);
        visit(child);
      }
    };
    visit(this);
    return matches;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] ?? null;
  }

  getBoundingClientRect() {
    return { left: 0, top: 0, width: 780, height: 220 };
  }

  setPointerCapture() {}
}

class FakeDocument extends FakeNode {
  constructor() {
    super("document");
    for (const id of ["hive-map", "hive-details", "hive-status", "hive-alert-list", "hive-refresh", "hive-motion"]) {
      this.append(new FakeNode("div", id));
    }
  }

  createElement(tagName) {
    return new FakeNode(tagName);
  }

  createElementNS(_namespace, tagName) {
    return new FakeNode(tagName);
  }

  createTextNode(value) {
    const node = new FakeNode("span");
    node.textContent = value;
    return node;
  }
}

function response(payload) {
  return { ok: true, json: async () => payload };
}

function snapshot(alerts) {
  return {
    generated_at: "2026-09-29T12:00:00+00:00",
    regions: [{
      workflow_id: 1,
      revision: 1,
      name: "Delivery",
      initial: "wait",
      stations: [
        {
          id: "wait",
          title: "Depot",
          action: "wait_for_work",
          prompt: "Wait for work",
          skill: null,
          outcomes: [],
          layout: { x: 0, y: 0 },
          depot: true,
          terminal: false,
          agents_present: 0,
          steps_completed_24h: 2,
          recent_steps: [],
          duration: { typical_seconds: 20 },
          waiting_items: 2,
        },
        {
          id: "run",
          title: "Run skill",
          action: "run_skill",
          prompt: "Run the skill",
          skill: "deliver",
          outcomes: ["done"],
          layout: { x: 280, y: 0 },
          depot: false,
          terminal: false,
          agents_present: 1,
          steps_completed_24h: 3,
          recent_steps: [{ outcome: "done" }],
          duration: { typical_seconds: 20 },
          waiting_items: 0,
        },
      ],
      roads: [{ from: "wait", to: "run", outcome: "Todo" }],
      layout: { wait: { x: 0, y: 0 }, run: { x: 280, y: 0 } },
    }],
    units: [{
      agent_id: "agent-1",
      name: "worker",
      status: "working",
      region: 1,
      workflow_id: 1,
      workflow_revision: 1,
      station: "run",
      pass_id: "pass-1",
      item: { repository: "owner/repo", number: 231 },
      step_started_at: "2026-09-29T11:59:50+00:00",
      typical_seconds: 20,
      elapsed_seconds: 10,
      overdue: false,
      progress: 0.5,
      timing: { typical_seconds: 20 },
      current_step: { state_id: "run", status: "running", outcome: null, summary: "" },
      steps: [{ state_id: "run", status: "running", outcome: null, summary: "" }],
      color: "#e3b341",
      parked: false,
      stalled: false,
    }],
    alerts,
    waiting: { "1": 2 },
  };
}

test("Hive UI renders behavior, polls, acknowledges, and supports motion and viewport controls", async () => {
  const document = new FakeDocument();
  const activeAlert = {
    pass_id: "pass-1",
    agent_name: "worker",
    station_title: "Run skill",
    kind: "escalated",
    message: "operator review needed",
  };
  let currentSnapshot = snapshot([activeAlert]);
  let reducedMotion = true;
  let eventRequestCount = 0;
  let acknowledgementBody = null;
  const fetchCalls = [];
  const scheduled = [];
  const frames = [];
  const previousGlobals = {
    document: globalThis.document,
    window: globalThis.window,
    fetch: globalThis.fetch,
    setTimeout: globalThis.setTimeout,
    clearTimeout: globalThis.clearTimeout,
    requestAnimationFrame: globalThis.requestAnimationFrame,
  };
  const fetch = async (path, options = {}) => {
    fetchCalls.push({ path, options });
    if (path === "/api/hive") return response(currentSnapshot);
    if (path.startsWith("/api/hive/events")) {
      eventRequestCount += 1;
      return response({
        cursor: `cursor-${eventRequestCount}`,
        events: eventRequestCount === 1 ? [{ event: "step_finished", pass_id: "pass-1" }] : [],
      });
    }
    if (path.startsWith("/api/agents/agent-1/logs")) return response({ lines: ["log line"] });
    if (path === "/api/passes/pass-1/acknowledge") {
      acknowledgementBody = options.body;
      currentSnapshot = snapshot([]);
      return response({ pass_id: "pass-1", acknowledged_by: "operator" });
    }
    throw new Error(`unexpected fetch ${path}`);
  };
  globalThis.document = document;
  globalThis.window = { matchMedia: () => ({ matches: reducedMotion }) };
  globalThis.fetch = fetch;
  globalThis.setTimeout = (callback, delay) => {
    scheduled.push({ callback, delay });
    return scheduled.length;
  };
  globalThis.clearTimeout = () => {};
  globalThis.requestAnimationFrame = (callback) => {
    frames.push(callback);
    return frames.length;
  };
  try {
    const hive = await import(`../beehaiive/web/hive.js?behavior=${Date.now()}`);
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));

    assert.deepEqual(hive.pointFor(currentSnapshot.regions[0], "run", 40), { x: 390, y: 86 });
    assert.deepEqual(hive.regionSize(currentSnapshot.regions[0]), { width: 780, height: 160 });
    assert.deepEqual(hive.interpolatePosition({ x: 0, y: 10 }, { x: 100, y: 50 }, 0.25), { x: 25, y: 20 });

    const map = document.querySelector("#hive-map");
    const svg = map.querySelector("svg");
    const initialViewBox = svg.attributes.viewBox;
    map.querySelector('[aria-label="Zoom in"]').click();
    assert.notEqual(svg.attributes.viewBox, initialViewBox);
    svg.dispatch("pointerdown", { button: 0, pointerId: 1, clientX: 100, clientY: 100 });
    svg.dispatch("pointermove", { pointerId: 1, clientX: 60, clientY: 80 });
    svg.dispatch("pointerup", { pointerId: 1 });
    assert.notEqual(svg.attributes.viewBox, initialViewBox);

    const scheduledPoll = scheduled.shift();
    assert.equal(scheduledPoll.delay, 2000);
    await scheduledPoll.callback();
    await new Promise((resolve) => setImmediate(resolve));
    assert.ok(fetchCalls.some(({ path }) => path.startsWith("/api/hive/events?since=")));
    assert.equal(scheduled.at(-1).delay, 2000);
    assert.equal(frames.length, 0);

    const motionButton = document.querySelector("#hive-motion");
    motionButton.click();
    await new Promise((resolve) => setImmediate(resolve));
    reducedMotion = false;
    motionButton.click();
    await new Promise((resolve) => setImmediate(resolve));
    assert.ok(frames.length > 0);

    map.querySelector('[data-agent-id="agent-1"]').click();
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    assert.match(document.querySelector("#hive-details").textContent, /worker/);
    assert.match(document.querySelector("#hive-details").textContent, /log line/);

    map.querySelector('[data-station-id="run"]').click();
    assert.match(document.querySelector("#hive-details").textContent, /Run skill/);
    assert.match(document.querySelector("#hive-details").textContent, /deliver/);

    document.querySelector("#hive-alert-list").querySelector("button").click();
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(acknowledgementBody, JSON.stringify({ acknowledged_by: "operator" }));
    assert.equal(document.querySelector("#hive-alert-list").textContent, "No active alerts.");
  } finally {
    for (const [key, value] of Object.entries(previousGlobals)) {
      if (value === undefined) delete globalThis[key];
      else globalThis[key] = value;
    }
  }
});
