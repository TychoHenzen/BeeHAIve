export function createDashboardClient({
  fetcher,
  projectId,
  archived = () => false,
  apiKey,
  saveApiKey = () => {},
  onState = () => {},
  onStatus = () => {},
  onBusy = () => {},
}) {
  let stateRevision = 0;
  let refreshController = null;
  let actionPending = false;
  let actionProject = "";
  let currentState = null;
  let stateProject = "";

  async function refresh() {
    const currentProject = projectId().trim();
    if (actionPending) {
      if (currentProject !== actionProject) {
        stateRevision += 1;
      }
      return null;
    }
    const revision = ++stateRevision;
    refreshController?.abort();
    refreshController = null;
    if (stateProject && stateProject !== currentProject) {
      currentState = null;
      stateProject = currentProject;
      onState({ project_id: currentProject, repositories: [], actions: [], counts: {} });
    }
    if (!currentProject) {
      onStatus("Enter a project ID to load live state.");
      return null;
    }
    const controller = new AbortController();
    refreshController = controller;
    onStatus("Loading live state...", "pending");
    try {
      const archiveQuery = archived() ? "?archived=true" : "";
      const response = await fetcher(
        `/projects/${encodeURIComponent(currentProject)}/dashboard${archiveQuery}`,
        { cache: "no-store", signal: controller.signal },
      );
      const payload = await readJson(response);
      if (revision !== stateRevision) return null;
      currentState = payload;
      stateProject = currentProject;
      onState(payload);
      onStatus(`Updated ${payload.updated_at || "now"}.`, "success");
      return payload;
    } catch (error) {
      if (error.name === "AbortError" || revision !== stateRevision) return null;
      onStatus(`Live state failed: ${error.message}`, "failure");
      return null;
    } finally {
      if (refreshController === controller) refreshController = null;
    }
  }

  async function runAction(payload) {
    if (actionPending) return null;
    actionPending = true;
    actionProject = projectId().trim();
    const revision = ++stateRevision;
    refreshController?.abort();
    refreshController = null;
    onBusy(true);
    onStatus(`${payload.action} pending...`, "pending");
    const headers = { "Content-Type": "application/json" };
    const configuredApiKey = apiKey();
    try {
      if (configuredApiKey) {
        headers["X-API-Key"] = configuredApiKey;
        saveApiKey(configuredApiKey);
      }
      const archiveQuery = archived() ? "?archived=true" : "";
      const response = await fetcher(
        `/projects/${encodeURIComponent(projectId().trim())}/actions${archiveQuery}`,
        {
          method: "POST",
          headers,
          body: JSON.stringify({ ...payload, approved: true }),
        },
      );
      const result = await readJson(response);
      if (revision !== stateRevision) return null;
      if (result.state) {
        currentState = result.state;
        stateProject = actionProject;
        onState(result.state);
      } else if (currentState) {
        currentState = {
          ...currentState,
          actions: [...(currentState.actions || []), result.action].slice(-50),
        };
        onState(currentState);
      } else {
        currentState = {
          project_id: actionProject,
          repositories: [],
          counts: {},
          actions: [result.action],
        };
        stateProject = actionProject;
        onState(currentState);
      }
      if (result.action.status === "failed") {
        onStatus(`${payload.action} failed: ${result.action.error}`, "failure");
      } else {
        onStatus(`${payload.action} succeeded.`, "success");
      }
      return result;
    } catch (error) {
      onStatus(`${payload.action} failed: ${error.message}`, "failure");
      return null;
    } finally {
      actionPending = false;
      const changedProject = projectId().trim() !== actionProject;
      actionProject = "";
      onBusy(false);
      if (changedProject) void refresh();
    }
  }

  return { refresh, runAction };
}

async function readJson(response) {
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.detail || `Request failed (${response.status})`);
  }
  return payload;
}
