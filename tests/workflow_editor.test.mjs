import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import test from "node:test";

const appSource = await readFile(new URL("../beehaiive/web/app.js", import.meta.url), "utf8");

function camelCase(attribute) {
  return attribute.replace(/^data-/, "").replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
}

function selectorMatches(node, selector) {
  if (selector.startsWith("#")) return node.id === selector.slice(1);
  if (selector.startsWith(".")) return String(node.className).split(/\s+/).includes(selector.slice(1));
  const match = selector.match(/^\[([^=\]]+)(?:=\"([^\"]*)\")?\]$/);
  if (!match) return false;
  const [, attribute, expected] = match;
  const value = attribute.startsWith("data-")
    ? node.dataset[camelCase(attribute)]
    : node.attributes[attribute];
  return expected === undefined ? value !== undefined : String(value) === expected;
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
    this.disabled = false;
    this.checked = false;
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
      toggle: (name, force) => {
        const hasName = this.className.split(/\s+/).includes(name);
        const shouldHave = force ?? !hasName;
        if (shouldHave && !hasName) this.className = `${this.className} ${name}`.trim();
        if (!shouldHave && hasName) this.className = this.className.split(/\s+/).filter((item) => item !== name).join(" ");
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
    const listener = this.listeners.get(type);
    if (listener) listener({ target: this, ...event });
  }

  click() {
    this.dispatch("click");
  }

  setAttribute(attribute, value) {
    this.attributes[attribute] = String(value);
    if (attribute.startsWith("data-")) this.dataset[camelCase(attribute)] = String(value);
  }

  removeAttribute(attribute) {
    delete this.attributes[attribute];
    if (attribute.startsWith("data-")) delete this.dataset[camelCase(attribute)];
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
    return { left: 0, top: 0 };
  }

  setPointerCapture() {}

  focus() {}
}

class FakeDocument extends FakeNode {
  constructor() {
    super("document");
    for (const id of [
      "board", "snapshot-status", "workflow-list", "workflow-editor", "workflow-status",
      "workflow-name", "workflow-prompt", "workflow-parameters", "workflow-canvas",
      "workflow-json", "workflow-errors", "workflow-save", "refresh", "workflow-new",
      "workflow-example", "workflow-return", "workflow-generate", "workflow-add-state",
      "workflow-add-transition", "workflow-add-parameter", "workflow-help", "workflow-advanced",
      "agents-refresh", "agent-new", "agent-form", "agent-form-title", "agent-name",
      "agent-workflow", "agent-parameters", "agent-repository", "agent-checkout",
      "agent-model", "agent-save", "agent-cancel", "agent-list", "agents-status",
    ]) {
      this.append(new FakeNode("div", id));
    }
  }

  querySelector(selector) {
    if (selector.startsWith("#")) {
      return this.children.find((child) => child.id === selector.slice(1)) ?? null;
    }
    return super.querySelector(selector);
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

function invalidValidation() {
  return {
    valid: false,
    errors: [
      { location: "states[wait]", message: "bad state", level: "error" },
      { location: "parameters[backlog]", message: "bad parameter", level: "error" },
      { location: "transitions[0].conditions", message: "bad edge", level: "error" },
    ],
    warnings: [],
  };
}

async function createHarness({ deferredValidation = false, agents = [], workflows = [] } = {}) {
  const document = new FakeDocument();
  const validationPayloads = [];
  const validationResolvers = [];
  let invalid = false;
  const fetch = async (path, options = {}) => {
    if (path === "/api/project") return response({ columns: [] });
    if (path === "/api/workflows/validate") {
      validationPayloads.push(JSON.parse(options.body));
      if (deferredValidation) {
        return new Promise((resolve) => validationResolvers.push(resolve));
      }
      return response(invalid ? invalidValidation() : { valid: true, errors: [], warnings: [] });
    }
    if (path === "/api/workflows") return response({ workflows });
    if (path.startsWith("/api/workflows/")) {
      const id = Number(path.split("/").at(-1));
      const workflow = workflows.find((candidate) => candidate.id === id);
      return response(workflow?.detail ?? { id, latest: { definition: {} } });
    }
    if (path === "/api/agents") return response({ agents });
    throw new Error(`unexpected fetch ${path}`);
  };
  const context = {
    document,
    fetch,
    structuredClone,
    console,
    setTimeout,
    clearTimeout,
    Promise,
    JSON,
    Date,
    Map,
    Set,
    Math,
    String,
    Number,
    Error,
  };
  vm.runInNewContext(appSource, context, { filename: "app.js" });
  await new Promise((resolve) => setImmediate(resolve));
  return {
    document,
    validationPayloads,
    validationResolvers,
    setInvalid(value) { invalid = value; },
    resolveValidation(payload) {
      const resolver = validationResolvers.shift();
      assert.ok(resolver, "expected a pending validation request");
      resolver(response(payload));
    },
  };
}

function findText(node, text) {
  if (node.textContent === text) return node;
  for (const child of node.children) {
    const match = findText(child, text);
    if (match) return match;
  }
  return null;
}

test("workflow editor drives example, drag, duplicate, edge, and validation-pin behavior", async () => {
  const harness = await createHarness();
  const { document } = harness;
  document.querySelector("#workflow-example").click();
  await new Promise((resolve) => setImmediate(resolve));

  const canvas = document.querySelector("#workflow-canvas");
  assert.equal(canvas.querySelectorAll("[data-state-id]").length, 8);
  assert.equal(canvas.querySelectorAll("[data-transition-index]").length, 15);

  const wait = canvas.querySelector("[data-state-id=\"wait\"]");
  wait.dispatch("pointerdown", { pointerId: 1, clientX: 10, clientY: 10 });
  wait.dispatch("pointermove", { pointerId: 1, clientX: 90, clientY: 50 });
  wait.dispatch("pointerup", { pointerId: 1, clientX: 90, clientY: 50 });
  await new Promise((resolve) => setImmediate(resolve));
  const dragged = harness.validationPayloads.at(-1).definition.states.find((state) => state.id === "wait");
  assert.deepEqual(dragged.layout, { x: 80, y: 210 });

  let duplicate = findText(canvas.querySelector("[data-state-id=\"wait\"]"), "Duplicate");
  duplicate.click();
  duplicate = findText(document.querySelector("#workflow-canvas").querySelector("[data-state-id=\"wait\"]"), "Duplicate");
  duplicate.click();
  const ids = document.querySelector("#workflow-canvas").querySelectorAll("[data-state-id]").map((state) => state.dataset.stateId);
  assert.equal(new Set(ids).size, ids.length);

  document.querySelector("#workflow-add-transition").click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(document.querySelector("#workflow-canvas").querySelectorAll("[data-transition-index]").length, 16);
  const added = harness.validationPayloads.at(-1).definition.transitions.at(-1);
  assert.equal(added.conditions[0].kind, "item_status_is");

  harness.setInvalid(true);
  document.querySelector("#workflow-add-parameter").click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(document.querySelector("#workflow-save").disabled, true);
  assert.equal(document.querySelector("[data-state-id=\"wait\"]").attributes["aria-invalid"], "true");
  assert.equal(document.querySelector("[data-parameter-name=\"backlog\"]").attributes["aria-invalid"], "true");
  assert.equal(document.querySelector("[data-transition-index=\"0\"]").attributes["aria-invalid"], "true");
});

test("workflow editor ignores stale validation responses", async () => {
  const harness = await createHarness({ deferredValidation: true });
  const { document, validationResolvers } = harness;
  document.querySelector("#workflow-example").click();
  harness.resolveValidation({ valid: true, errors: [], warnings: [] });
  await new Promise((resolve) => setImmediate(resolve));

  const firstTitle = document.querySelector("[aria-label=\"wait title\"]");
  firstTitle.value = "older";
  firstTitle.dispatch("change");
  const secondTitle = document.querySelector("[aria-label=\"wait title\"]");
  secondTitle.value = "newer";
  secondTitle.dispatch("change");
  assert.equal(validationResolvers.length, 2);
  harness.resolveValidation(invalidValidation());
  harness.resolveValidation({ valid: true, errors: [], warnings: [] });
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(document.querySelector("#workflow-save").disabled, false);
  assert.equal(document.querySelector("#workflow-errors").textContent, "Definition is valid.");
});

test("agents view exposes assignment form and durable run evidence", async () => {
  const definition = {
    parameters: [
      { name: "skill", type: "skill", const: false, value: null },
      { name: "status", type: "status", const: true, value: "Todo" },
    ],
  };
  const harness = await createHarness({
    workflows: [{ id: 7, name: "Delivery", detail: { id: 7, latest: { definition } } }],
    agents: [{
      id: "agent-1",
      name: "runner",
      status: "stopped",
      workflow_id: 7,
      workflow_revision: 1,
      repository: "owner/repo",
      checkout_path: "C:/agents/runner",
      current_state: null,
      history: [{
        id: "pass-1",
        status: "completed",
        item: { title: "PBI" },
        steps: [{ sequence: 1, state_id: "run", status: "completed", outcome: "done", summary: "finished" }],
      }],
      alerts: [{ kind: "escalated", message: "needs operator" }],
    }],
  });
  const { document } = harness;
  document.querySelector("#agents-refresh").click();
  await new Promise((resolve) => setImmediate(resolve));

  assert.match(document.querySelector("#agent-list").textContent, /runner/);
  assert.match(document.querySelector("#agent-list").textContent, /run/);
  assert.match(document.querySelector("#agent-list").textContent, /needs operator/);

  document.querySelector("#agent-new").click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(document.querySelector("#agent-form").hidden, false);
  assert.equal(document.querySelector("#agent-parameters").querySelectorAll("[data-agent-parameter-name]").length, 1);
  assert.match(document.querySelector("#agent-parameters").textContent, /constant: Todo/);
});
