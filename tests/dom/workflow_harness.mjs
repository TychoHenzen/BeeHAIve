import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

import { FakeDocument } from "./fake_document.mjs";
import { FakeNode } from "./fake_node.mjs";
import { response, settle } from "./helpers.mjs";

const appSource = await readFile(new URL("../../beehaiive/web/app.js", import.meta.url), "utf8");

const ELEMENT_IDS = [
  "board", "snapshot-status", "workflow-list", "workflow-editor", "workflow-status",
  "workflow-name", "workflow-prompt", "workflow-parameters", "workflow-canvas",
  "workflow-json", "workflow-errors", "workflow-save", "refresh", "workflow-new",
  "workflow-example", "workflow-return", "workflow-generate", "workflow-add-state",
  "workflow-add-transition", "workflow-add-parameter", "workflow-help", "workflow-advanced",
  "agents-refresh", "agent-new", "agent-form", "agent-form-title", "agent-name",
  "agent-workflow", "agent-parameters", "agent-repository", "agent-checkout",
  "agent-model", "agent-save", "agent-cancel", "agent-list", "agents-status",
];
const TAB_NAMES = ["board", "workflows", "agents", "hive"];

function createWorkflowDocument() {
  const document = new FakeDocument(ELEMENT_IDS);
  for (const name of TAB_NAMES) {
    const tab = new FakeNode("button");
    tab.setAttribute("data-tab", name);
    if (name === "board") tab.className = "active";
    const panel = new FakeNode("section");
    panel.setAttribute("data-panel", name);
    if (name === "board") panel.className = "active";
    document.append(tab, panel);
  }
  return document;
}

export function validPayload() {
  return { valid: true, errors: [], warnings: [] };
}

export function invalidValidation() {
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

function workflowDetail(workflows, path) {
  const id = Number(path.split("/").at(-1));
  const workflow = workflows.find((candidate) => candidate.id === id);
  return response(workflow?.detail ?? { id, latest: { definition: {} } });
}

function createFetch(state) {
  return async (path, options = {}) => {
    if (path === "/api/project") return response({ columns: [] });
    if (path === "/api/workflows/validate") {
      state.validationPayloads.push(JSON.parse(options.body));
      if (state.deferredValidation) {
        return new Promise((resolve) => state.validationResolvers.push(resolve));
      }
      return response(state.invalid ? invalidValidation() : validPayload());
    }
    if (path === "/api/workflows") return response({ workflows: state.workflows });
    if (path.startsWith("/api/workflows/")) return workflowDetail(state.workflows, path);
    if (path === "/api/agents") return response({ agents: state.agents });
    throw new Error(`unexpected fetch ${path}`);
  };
}

function browserContext(document, fetch) {
  return {
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
}

export async function createHarness({
  deferredValidation = false,
  agents = [],
  workflows = [],
} = {}) {
  const state = {
    deferredValidation,
    agents,
    workflows,
    invalid: false,
    validationPayloads: [],
    validationResolvers: [],
  };
  const document = createWorkflowDocument();
  vm.runInNewContext(appSource, browserContext(document, createFetch(state)), {
    filename: "app.js",
  });
  await settle();
  return {
    document,
    validationPayloads: state.validationPayloads,
    validationResolvers: state.validationResolvers,
    setInvalid(value) {
      state.invalid = value;
    },
    resolveValidation(payload) {
      const resolver = state.validationResolvers.shift();
      assert.ok(resolver, "expected a pending validation request");
      resolver(response(payload));
    },
  };
}

export function findText(node, text) {
  if (node.textContent === text) return node;
  for (const child of node.children) {
    const match = findText(child, text);
    if (match) return match;
  }
  return null;
}
