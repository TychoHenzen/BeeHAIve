// How the console talks to its server. /archon/api/* is Archon's own API passed
// through BeeHAIve; /api/archon/* is what BeeHAIve adds through the Archon CLI.
// Every request that changes something carries X-BeeHAIve, which the server
// requires so another site cannot make the browser act here.

export class ApiError extends Error {
  constructor(status, message, detail) {
    super(detail ? `${message}\n\n${detail}` : message);
    this.status = status;
    this.detail = detail;
  }
}

async function request(url, { method = "GET", body } = {}) {
  const headers = { Accept: "application/json" };
  if (method !== "GET") headers["X-BeeHAIve"] = "1";
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(url, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  });
  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!response.ok) {
    const message = data?.error ?? data?.detail ?? (text.slice(0, 200) || `Request failed (${response.status})`);
    throw new ApiError(response.status, String(message), typeof data?.detail === "string" ? data.detail : "");
  }
  return data;
}

const enc = encodeURIComponent;
const archon = (path) => `/archon/api${path}`;

function query(params) {
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") qs.set(key, String(value));
  }
  const text = qs.toString();
  return text ? `?${text}` : "";
}

export const api = {
  health: () => request(archon("/health")),
  runs: (params = {}) => request(archon(`/dashboard/runs${query(params)}`)),
  run: (id) => request(archon(`/workflows/runs/${enc(id)}`)),
  artifacts: (id) => request(archon(`/runs/${enc(id)}/artifacts`)),
  artifactUrl: (id, path) => archon(`/artifacts/${enc(id)}/${path.split("/").map(enc).join("/")}`),
  // One node's model session, all of it or what follows `after` ({ attemptId, seq }).
  providerEvents: (id, step, after = null) =>
    request(archon(`/workflows/runs/${enc(id)}/provider-events${query({ step, attemptId: after?.attemptId, afterSeq: after?.seq })}`)),
  attention: () => request(archon("/runs/attention?all=true")),
  projects: () => request(archon("/codebases")),
  workflows: (cwd) => request(archon(`/workflows${query({ cwd })}`)),
  cancel: (id) => request(archon(`/workflows/runs/${enc(id)}/cancel`), { method: "POST" }),
  resume: (id) => request(archon(`/workflows/runs/${enc(id)}/resume`), { method: "POST" }),
  abandon: (id) => request(archon(`/workflows/runs/${enc(id)}/abandon`), { method: "POST" }),
  respond: (id, body) => request(archon(`/workflows/runs/${enc(id)}/respond`), { method: "POST", body }),
  answer: (id, body) => request(archon(`/workflows/runs/${enc(id)}/answer`), { method: "POST", body }),
  async startRun({ projectId, workflow, message, inputs }) {
    const conversation = await request(archon("/conversations"), { method: "POST", body: { codebaseId: projectId } });
    const body = { conversationId: conversation.conversationId, message };
    if (inputs && Object.keys(inputs).length > 0) body.inputs = inputs;
    return request(archon(`/workflows/${enc(workflow)}/run`), { method: "POST", body });
  },

  settings: () => request("/api/archon/settings"),
  identity: () => request("/api/archon/identity"),
  bindings: () => request("/api/archon/triggers/bindings"),
  createBinding: (form) => request("/api/archon/triggers/bindings", { method: "POST", body: form }),
  // Everything but the name and run-as user, which an edit keeps.
  updateBinding: (name, changes) => request(`/api/archon/triggers/bindings/${enc(name)}`, { method: "PUT", body: changes }),
  deleteBinding: (name) => request(`/api/archon/triggers/bindings/${enc(name)}`, { method: "DELETE" }),
  fire: (name) => request(`/api/archon/triggers/bindings/${enc(name)}/fire`, { method: "POST" }),
  // How each binding's newest fire went, read from its receipt: the listing
  // says "matched" even for a fire Archon then refused.
  triggerHealth: () => request("/api/archon/triggers/health"),
  receipts: (limit = 50) => request(`/api/archon/triggers/receipts${query({ limit })}`),
  receipt: (id) => request(`/api/archon/triggers/receipts/${enc(id)}`),
  schedules: () => request("/api/archon/triggers/schedules"),
  installSchedule: (name) => request(`/api/archon/triggers/bindings/${enc(name)}/schedule`, { method: "PUT" }),
  removeSchedule: (name) => request(`/api/archon/triggers/bindings/${enc(name)}/schedule`, { method: "DELETE" }),
  // The repositories a pbi queue serves, with the last reading of `repository`
  // if one is under five minutes old. Only the POST reads again.
  pipeline: (repository) => request(`/api/archon/pipeline${query({ repository })}`),
  readPipeline: (repository) => request("/api/archon/pipeline", { method: "POST", body: { repository } }),
};

// One shared EventSource on Archon's dashboard stream. Its frames only say that a
// run changed: the same transition can arrive twice and a stale one can arrive
// late, so pages refetch what they show instead of trusting the payloads.
const CHANGE_TYPES = new Set(["workflow_status", "dag_node"]);

export function liveFeed(onState) {
  const listeners = new Set();
  const changed = new Set();
  let timer = null;
  const source = new EventSource(archon("/stream/__dashboard__"));
  source.onopen = () => onState("live");
  source.onmessage = (event) => {
    let frame = null;
    try {
      frame = JSON.parse(event.data);
    } catch {
      return;
    }
    if (!CHANGE_TYPES.has(frame?.type)) return;
    if (typeof frame.runId === "string") changed.add(frame.runId);
    clearTimeout(timer);
    timer = setTimeout(() => {
      const runIds = [...changed];
      changed.clear();
      listeners.forEach((fn) => fn(runIds));
    }, 400);
  };
  source.onerror = () => onState(source.readyState === EventSource.CLOSED ? "offline" : "reconnecting");
  return {
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
}
