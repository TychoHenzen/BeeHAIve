const STAGES = [
  ["backlog", "Plan"],
  ["refine", "Refine"],
  ["implement", "Build"],
  ["review", "Review"],
  ["pull_request", "Pull request"],
  ["merge", "Ship"],
];

const AUTONOMOUS_SKILLS = [
  "refine-backlog-item",
  "next-ticket",
  "submit-draft-pr",
  "review-pr-branch",
  "fix-pr-review",
  "complete-pr",
];

function progress(stage, completed = false) {
  const current = STAGES.findIndex(([id]) => id === stage);
  return STAGES.map(([id, label], index) => ({
    id,
    label,
    status: completed || index < current ? "complete" : index === current ? "current" : "pending",
  }));
}

function now() {
  return new Date().toISOString();
}

function demoGraph() {
  return {
    workflow_id: "demo-flow",
    definitions: [{
      workflow_id: "demo-flow",
      revision: 1,
      schema_version: "1",
      definition_hash: "demo-revision-1",
      active: true,
      review: true,
      nodes: [
        { node_id: "claim", kind: "scheduler" },
        { node_id: "refine", kind: "agent" },
        { node_id: "implement", kind: "agent" },
        { node_id: "review", kind: "approval" },
        { node_id: "deliver", kind: "delivery" },
      ],
      edges: [
        { source: "claim", target: "refine", condition: "claimable" },
        { source: "refine", target: "implement", condition: "plan approved" },
        { source: "implement", target: "review", condition: "checks pass" },
        { source: "review", target: "deliver", condition: "review approved" },
      ],
      safety_evidence: {
        evidence: {
          safety: {
            passed: true,
            checks: [{ name: "reachability", status: "pass", reason: "All nodes are reachable." }],
          },
        },
      },
    }],
    active: { workflow_id: "demo-flow", revision: 1 },
    empty: false,
  };
}

function pbi(number, title, stage, status, extra = {}) {
  return {
    id: `demo/app#${number}`,
    number,
    title,
    stage,
    stage_label: STAGES.find(([id]) => id === stage)?.[1] || "Plan",
    stage_progress: progress(stage, status === "completed"),
    status,
    active: status !== "idle" || extra.claimable === true,
    attempt: status === "idle" ? null : 1,
    claimable: extra.claimable === true,
    run_id: extra.run_id || null,
    operator_questions: extra.operator_questions || [],
    pull_requests: extra.pull_requests || [],
    checks: extra.checks || { verdict: "unproven", pull_requests: [] },
    subtasks: extra.subtasks || [],
    activity: extra.activity || [],
    result: extra.result || null,
    ...extra,
  };
}

function initialState() {
  return {
    project_id: "demo:1",
    name: "Guided delivery demo",
    projects: ["beehaive", "omelette", "nocturne"],
    workflow_ids: ["demo-flow"],
    updated_at: now(),
    repositories: [
      {
        name: "demo/app",
        project: "beehaive",
        active: true,
        writer: { status: "idle", current_pbi: null },
        pbis: [
          pbi(101, "Add account recovery", "backlog", "idle", {
            claimable: true,
            subtasks: [{ id: "design", title: "Confirm recovery email copy" }],
          }),
        ],
      },
      {
        name: "demo/api",
        project: "omelette",
        active: true,
        writer: { status: "idle", current_pbi: null },
        pbis: [
          pbi(102, "Harden session expiry", "pull_request", "idle", {
            pull_requests: [{ number: 17, url: "https://example.test/demo/api/pull/17", state: "open" }],
            checks: { verdict: "passing", pull_requests: [] },
            reviewers: { security: { status: "pass" }, tests: { status: "pass" } },
          }),
        ],
      },
      {
        name: "demo/worker",
        project: "nocturne",
        active: true,
        writer: { status: "idle", current_pbi: null },
        pbis: [
          pbi(103, "Record delivery evidence", "merge", "completed", {
            archived: true,
            result: "Merged after required checks passed.",
            pull_requests: [{ number: 12, url: "https://example.test/demo/worker/pull/12", state: "closed", merged: true }],
          }),
        ],
      },
      {
        name: "demo/ops",
        project: "beehaive",
        active: true,
        writer: { status: "active", current_pbi: 104 },
        pbis: [
          pbi(104, "Choose deployment target", "implement", "awaiting_operator", {
            run_id: "demo-question-104",
            operator_questions: [{
              question_id: "demo-question",
              revision: 1,
              status: "pending",
              question: "Which environment should receive the preview build?",
            }],
          }),
        ],
      },
    ],
    actions: [],
    scheduler: {
      enabled: false,
      running: false,
      poll_interval_seconds: 600,
      max_concurrency: 1,
      active_workers: 0,
      last_poll_at: null,
      last_error: null,
    },
    graph: demoGraph(),
    graphs: { "demo-flow": demoGraph() },
  };
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

export function createDemoClient({ onState, onStatus, onBusy, archived = () => false, workflowId = () => "" }) {
  let state = initialState();

  function visibleState() {
    const snapshot = clone(state);
    const selectedWorkflow = workflowId().trim();
    const selectedGraph = state.graphs?.[selectedWorkflow];
    if (selectedGraph) snapshot.graph = clone(selectedGraph);
    snapshot.archived = Boolean(archived());
    snapshot.recent_deliveries = state.repositories.flatMap((repository) => repository.pbis
      .filter((item) => Boolean(item.archived) || item.status === "completed" || item.pull_requests?.length || item.delivery)
      .map((item) => ({ repository: repository.name, project: repository.project, pbi: clone(item) })));
    snapshot.repositories.forEach((repository) => {
      repository.pbis = repository.pbis.filter((item) => Boolean(item.archived) === Boolean(archived()));
    });
    if (selectedWorkflow && selectedWorkflow !== snapshot.graph.workflow_id) {
      snapshot.graph = {
        workflow_id: selectedWorkflow,
        definitions: [],
        active: null,
        empty: true,
        message: "No graph definitions available for this workflow.",
      };
    }
    return snapshot;
  }

  function findItem(payload) {
    const repository = state.repositories.find((item) => item.name === payload.repository);
    if (!repository) return null;
    const target = payload.pbi_number === undefined
      ? repository.pbis.find((item) => item.run_id === payload.run_id)
        || repository.pbis.find((item) => item.claimable && !item.run_id)
      : repository.pbis.find((item) => item.number === payload.pbi_number);
    return target ? { repository, pbi: target } : null;
  }

  function record(payload, status = "succeeded", result = null) {
    state.actions.push({
      kind: payload.action,
      status,
      repository: payload.repository,
      pbi_number: payload.pbi_number,
      request: { ...payload },
      result,
      created_at: now(),
    });
    state.updated_at = now();
  }

  function addActivity(item, action) {
    item.pbi.activity = [...(item.pbi.activity || []), { created_at: now(), action }];
  }

  function refresh() {
    onStatus("Demo data ready.", "success");
    onState(visibleState());
    return Promise.resolve(visibleState());
  }

  async function runAction(payload) {
    onBusy(true);
    onStatus(`${payload.action} pending...`, "pending");
    if (payload.action.startsWith("graph_")) {
      let graph = state.graphs?.[payload.workflow_id] || state.graph;
      if (payload.action === "graph_evaluate" && payload.workflow_id && payload.workflow_id !== graph.workflow_id) {
        const candidate = clone(payload.candidate || {});
        graph = {
          workflow_id: payload.workflow_id,
          definitions: [{
            ...candidate,
            definition_hash: `demo-${payload.workflow_id}-1`,
            active: false,
            review: false,
            safety_evidence: { evidence: { safety: { passed: true, checks: [{ name: "reachability", status: "pass", reason: "All nodes are reachable." }] } } },
          }],
          active: null,
          empty: false,
        };
        state.graph = graph;
        state.graphs[payload.workflow_id] = graph;
        if (!state.workflow_ids.includes(payload.workflow_id)) state.workflow_ids.push(payload.workflow_id);
      }
      let latest = graph.definitions[graph.definitions.length - 1];
      if (payload.action === "graph_evaluate" && payload.candidate && Number(payload.candidate.revision) > Number(latest?.revision || 0)) {
        graph.definitions.push({
          ...clone(payload.candidate),
          active: false,
          review: false,
          definition_hash: `demo-${graph.workflow_id}-${payload.candidate.revision}`,
        });
        latest = graph.definitions[graph.definitions.length - 1];
      }
      if (payload.action === "graph_evaluate" && latest) {
        latest.safety_evidence = {
          evidence: {
            safety: {
              passed: true,
              checks: [{ name: "reachability", status: "pass", reason: "All nodes are reachable." }],
            },
          },
        };
      } else if (payload.action === "graph_review" && latest) {
        latest.review = true;
      } else if (payload.action === "graph_activate" && latest) {
        latest.active = true;
        graph.active = { workflow_id: graph.workflow_id, revision: latest.revision };
      } else if (payload.action === "graph_rollback") {
        graph.active = { workflow_id: graph.workflow_id, revision: payload.revision };
      }
      state.graphs[graph.workflow_id] = graph;
      state.graph = graph;
      record(payload);
      onState(visibleState());
      onStatus(`${payload.action} succeeded.`, "success");
      onBusy(false);
      return { action: { kind: payload.action, status: "succeeded" }, state: visibleState() };
    }
    const item = findItem(payload);
    if (!item && payload.action !== "sync") {
      record(payload, "failed");
      onStatus(`${payload.action} failed: Work item not found`, "failure");
      onState(visibleState());
      onBusy(false);
      return null;
    }
    if (payload.action === "start") {
      item.pbi.status = "active";
      item.pbi.active = true;
      item.pbi.claimable = false;
      item.pbi.stage = "refine";
      item.pbi.stage_label = "Refine";
      item.pbi.stage_progress = progress("refine");
      item.pbi.run_id = `demo-run-${item.pbi.number}`;
      item.pbi.attempt = 1;
      item.repository.writer = { status: "active", current_pbi: item.pbi.number };
      addActivity(item, "Started refinement");
    } else if (payload.action === "advance") {
      item.pbi.stage = "implement";
      item.pbi.stage_label = "Build";
      item.pbi.stage_progress = progress("implement");
      addActivity(item, "Approved plan and started build");
    } else if (payload.action === "approve" && item.pbi.stage === "implement") {
      item.pbi.status = "idle";
      item.pbi.active = true;
      item.pbi.stage = "pull_request";
      item.pbi.stage_label = "Pull request";
      item.pbi.stage_progress = progress("pull_request");
      item.pbi.pull_requests = [{ number: 41, url: "https://example.test/demo/app/pull/41", state: "open" }];
      item.pbi.checks = { verdict: "passing", pull_requests: [] };
      item.pbi.run_id = null;
      item.repository.writer = { status: "idle", current_pbi: null };
      addActivity(item, "Pull request opened");
    } else if (payload.action === "approve" && item.pbi.stage === "pull_request") {
      item.pbi.status = "active";
      item.pbi.stage = "merge";
      item.pbi.stage_label = "Ship";
      item.pbi.stage_progress = progress("merge");
      item.pbi.run_id = `demo-merge-${item.pbi.number}`;
      item.repository.writer = { status: "active", current_pbi: item.pbi.number };
      addActivity(item, "Review approved");
    } else if (payload.action === "merge") {
      item.pbi.status = "completed";
      item.pbi.active = false;
      item.pbi.stage = "merge";
      item.pbi.stage_label = "Ship";
      item.pbi.stage_progress = progress("merge", true);
      item.pbi.result = "Merged after required checks passed.";
      item.pbi.archived = true;
      item.pbi.pull_requests = [{ number: 41, url: "https://example.test/demo/app/pull/41", state: "closed", merged: true }];
      item.pbi.run_id = null;
      item.repository.writer = { status: "idle", current_pbi: null };
      addActivity(item, "Delivery confirmed");
    } else if (payload.action === "answer_question") {
      const question = item.pbi.operator_questions.find((value) => value.question_id === payload.question_id);
      if (question) {
        question.status = "answered";
        question.answer = payload.answer;
      }
      item.pbi.status = "active";
      addActivity(item, "Operator answered deployment question");
    } else if (payload.action === "clarify") {
      addActivity(item, "Clarification sent to worker");
    } else if (payload.action === "stop") {
      item.pbi.status = "failed";
      item.pbi.claimable = true;
      item.pbi.last_error = "Stopped by operator";
      item.repository.writer = { status: "idle", current_pbi: null };
      addActivity(item, "Work stopped");
    } else if (payload.action === "retry") {
      item.pbi.status = "active";
      item.pbi.claimable = false;
      item.pbi.last_error = null;
      item.repository.writer = { status: "active", current_pbi: item.pbi.number };
      addActivity(item, "Work retried");
    }
    record(payload);
    onState(visibleState());
    onStatus(`${payload.action} succeeded.`, "success");
    onBusy(false);
    return { action: { kind: payload.action, status: "succeeded" }, state: visibleState() };
  }

  async function runAutonomous(payload) {
    onBusy(true);
    onStatus("autonomous lifecycle pending...", "pending");
    const item = findItem(payload);
    if (!item) {
      record({ ...payload, action: "autonomous_start" }, "failed");
      onStatus("autonomous lifecycle failed: Work item not found", "failure");
      onState(visibleState());
      onBusy(false);
      return null;
    }
    const handoffs = AUTONOMOUS_SKILLS.map((skill, index) => ({
      step: skill,
      status: "succeeded",
      summary: `Placeholder context completed ${skill}.`,
      handover: {
        project_id: state.project_id,
        repository: item.repository.name,
        pbi_number: item.pbi.number,
        single_branch: true,
        subtasks_same_branch: true,
        next_skill: AUTONOMOUS_SKILLS[index + 1] || "complete",
      },
    }));
    item.pbi.status = "completed";
    item.pbi.active = false;
    item.pbi.claimable = false;
    item.pbi.archived = true;
    item.pbi.stage = "merge";
    item.pbi.stage_label = "Ship";
    item.pbi.stage_progress = progress("merge", true);
    item.pbi.result = "Placeholder lifecycle completed through published review and PR completion.";
    item.pbi.autonomous_handoffs = handoffs;
    item.pbi.pull_requests = [{ number: 41, url: "https://example.test/demo/app/pull/41", state: "closed", merged: true }];
    item.repository.writer = { status: "idle", current_pbi: null };
    handoffs.forEach((handoff) => record({ action: `skill:${handoff.step}`, repository: item.repository.name, pbi_number: item.pbi.number }));
    record({ action: "autonomous_complete", repository: item.repository.name, pbi_number: item.pbi.number });
    addActivity(item, "Autonomous lifecycle completed");
    onState(visibleState());
    onStatus("autonomous lifecycle completed.", "success");
    onBusy(false);
    return { run_id: `demo-autonomous-${item.pbi.number}`, status: "completed", state: visibleState() };
  }

  async function configureScheduler(config) {
    onBusy(true);
    onStatus("scheduler configuration pending...", "pending");
    state.scheduler = {
      ...state.scheduler,
      ...config,
      running: config.enabled,
      active_workers: 0,
    };
    record({ action: "scheduler_configure" });
    onState(visibleState());
    onStatus("scheduler settings applied.", "success");
    onBusy(false);
    return { scheduler: clone(state.scheduler), status: config.enabled ? "running" : "off" };
  }

  return { refresh, runAction, runAutonomous, configureScheduler };
}
