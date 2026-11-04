export function activeAlert() {
  return {
    pass_id: "pass-1",
    agent_name: "worker",
    station_title: "Run skill",
    kind: "escalated",
    message: "operator review needed",
  };
}

function depotStation() {
  return {
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
  };
}

function runStation() {
  return {
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
  };
}

function deliveryRegion() {
  return {
    workflow_id: 1,
    revision: 1,
    name: "Delivery",
    initial: "wait",
    stations: [depotStation(), runStation()],
    roads: [{ from: "wait", to: "run", outcome: "Todo" }],
    layout: { wait: { x: 0, y: 0 }, run: { x: 280, y: 0 } },
  };
}

function workingUnit() {
  const runningStep = { state_id: "run", status: "running", outcome: null, summary: "" };
  return {
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
    current_step: { ...runningStep },
    steps: [{ ...runningStep }],
    color: "#e3b341",
    parked: false,
    stalled: false,
  };
}

export function snapshot(alerts) {
  return {
    generated_at: "2026-09-29T12:00:00+00:00",
    regions: [deliveryRegion()],
    units: [workingUnit()],
    alerts,
    waiting: { "1": 2 },
  };
}
