const MAX_GRAPH_ITEMS = 100;
const MAX_GRAPH_NODES = 64;
const MAX_GRAPH_EDGES = 128;
const MAX_GRAPH_DOCUMENT_TEXT = 64_000;

export function createGraphRenderer(dom, runAction) {
  const { element } = dom;
  let draftScope = "";
  let draftValues = {};

  function display(value) {
    return value === undefined || value === null || value === "" ? "Unavailable" : value;
  }

  function objectInput(label, value, draftKey, scope, className) {
    const wrapper = element("label", undefined, "graph-input");
    wrapper.append(element("span", label));
    const input = element("textarea");
    input.setAttribute?.("aria-label", label);
    if (draftScope === scope && Object.hasOwn(draftValues, draftKey)) {
      input.value = draftValues[draftKey];
    } else {
      input.value = value;
    }
    input.maxLength = MAX_GRAPH_DOCUMENT_TEXT;
    input.addEventListener?.("input", () => {
      if (draftScope !== scope) {
        draftScope = scope;
        draftValues = {};
      }
      draftValues[draftKey] = input.value;
    });
    wrapper.append(input);
    if (className) wrapper.className = `${wrapper.className} ${className}`;
    return input;
  }

  function parseObject(input, label) {
    try {
      const value = JSON.parse(input.value || "{}");
      if (!value || typeof value !== "object" || Array.isArray(value)) {
        throw new Error(`${label} must be a JSON object`);
      }
      return value;
    } catch (error) {
      throw new Error(`${label}: ${error.message}`);
    }
  }

  function renderDefinition(definition, active, workflowId) {
    const section = element("section");
    const revision = definition.revision ?? "?";
    section.append(element("h3", `Revision ${revision}${definition.active ? " · active" : ""}`));
    section.append(element("div", `Workflow: ${workflowId}`, "mono"));
    section.append(element("div", `Definition hash: ${display(definition.definition_hash)}`, "muted"));
    const nodes = Array.isArray(definition.nodes) ? definition.nodes.slice(0, MAX_GRAPH_NODES) : [];
    const nodeList = nodes.map((node) => `${node.node_id || "unknown"} (${node.kind || "unknown"})`);
    section.append(element("div", `Nodes: ${nodeList.join(", ") || "None"}`, "muted"));
    const edges = Array.isArray(definition.edges) ? definition.edges.slice(0, MAX_GRAPH_EDGES) : [];
    const edgeList = edges.map((edge) => `${edge.source || "?"} -> ${edge.target || "?"} [${edge.condition || "always"}]`);
    section.append(element("div", `Edges: ${edgeList.join(", ") || "None"}`, "muted"));
    const evidence = definition.safety_evidence;
    const safety = evidence && typeof evidence === "object"
      ? evidence.evidence?.safety || evidence.safety
      : null;
    section.append(element("div", `Safety: ${safety ? (safety.passed ? "passing" : "blocking") : "unavailable"}`, "muted"));
    if (safety && Array.isArray(safety.checks)) {
      const checks = element("ul");
      safety.checks.slice(0, MAX_GRAPH_ITEMS).forEach((check) => {
        checks.append(element("li", `${check.name || "check"}: ${check.status || "unknown"} · ${check.reason || "reason unavailable"}`, "muted"));
      });
      section.append(checks);
    }
    const simulation = evidence && typeof evidence === "object"
      ? evidence.evidence?.simulation
      : null;
    const comparison = evidence && typeof evidence === "object"
      ? evidence.evidence?.comparison
      : null;
    if (simulation) section.append(element("div", `Simulation: ${simulation.complete ? "complete" : "incomplete"}`, "muted"));
    if (comparison) section.append(element("div", `Comparison: ${comparison.complete ? "complete" : "incomplete"}`, "muted"));
    section.append(element("div", `Review: ${definition.review ? "approved" : "pending"}`, "muted"));
    if (active && revision < active.revision && definition.review) {
      const rollback = element("button", `Rollback to revision ${revision}`, "secondary");
      rollback.type = "button";
      rollback.addEventListener("click", () => runAction({
        action: "graph_rollback",
        workflow_id: workflowId,
        revision,
      }));
      section.append(rollback);
    }
    return section;
  }

  function renderGraph(graph, projectId = "") {
    const section = element("section", undefined, "graph-editor");
    section.append(element("h2", "Workflow graph"));
    if (!graph || !graph.workflow_id) {
      section.append(element("div", "Enter a workflow ID to view graph state.", "empty"));
      return section;
    }
    const workflowId = String(graph.workflow_id);
    const scope = `${projectId}:${workflowId}`;
    if (draftScope && draftScope !== scope) draftValues = {};
    draftScope = scope;
    section.append(element("div", `Workflow: ${workflowId}`, "mono"));
    const active = graph.active && typeof graph.active === "object" ? graph.active : null;
    section.append(element("div", `Active revision: ${active ? display(active.revision) : "None"}`, "muted"));
    const definitions = Array.isArray(graph.definitions) ? graph.definitions.slice(-MAX_GRAPH_ITEMS) : [];
    if (definitions.length === 0) {
      section.append(element("div", graph.message || "No graph definitions available.", "empty"));
    } else {
      const list = element("div");
      definitions.forEach((definition) => list.append(renderDefinition(definition, active, workflowId)));
      section.append(list);
    }

    const definitionDocument = (definition) => {
      if (!definition || typeof definition !== "object") return {};
      return Object.fromEntries(
        ["workflow_id", "revision", "schema_version", "nodes", "edges", "model_policy", "limits", "metadata"]
          .filter((key) => definition[key] !== undefined)
          .map((key) => [key, definition[key]]),
      );
    };
    const latest = definitions.length > 0
      ? definitionDocument(definitions[definitions.length - 1])
      : {};
    const previous = definitions.length > 1
      ? definitionDocument(definitions[definitions.length - 2])
      : {};
    const candidate = objectInput(
      "Graph draft JSON",
      JSON.stringify(latest, null, 2),
      "candidate",
      scope,
    );
    const fixtures = objectInput(
      "Simulation fixtures JSON",
      "{}",
      "fixtures",
      scope,
    );
    const baseline = objectInput(
      "Baseline graph JSON (for revision comparison)",
      JSON.stringify(previous, null, 2),
      "baseline",
      scope,
    );
    const baselineFixtures = objectInput(
      "Baseline fixtures JSON",
      "{}",
      "baselineFixtures",
      scope,
    );
    section.append(candidate, fixtures, baseline, baselineFixtures);
    const controls = element("div", undefined, "actions");
    [
      ["Evaluate draft", "graph_evaluate"],
      ["Record review", "graph_review"],
      ["Activate reviewed draft", "graph_activate"],
    ].forEach(([label, action]) => {
      const button = element("button", label, "secondary");
      button.type = "button";
      button.addEventListener("click", () => {
        try {
          const baselineValue = parseObject(baseline, "Baseline graph");
          const payload = {
            action,
            workflow_id: workflowId,
            candidate: parseObject(candidate, "Graph draft"),
            fixtures: parseObject(fixtures, "Simulation fixtures"),
          };
          if (Object.keys(baselineValue).length > 0) {
            payload.baseline = baselineValue;
            payload.baseline_fixtures = parseObject(baselineFixtures, "Baseline fixtures");
          }
          runAction(payload);
        } catch (error) {
          section.append(element("p", error.message, "status failure"));
        }
      });
      controls.append(button);
    });
    section.append(controls);
    return section;
  }

  return renderGraph;
}
