export function createDashboardClient({
  fetcher,
  projectId,
  apiKey,
  saveApiKey = () => {},
  onState = () => {},
  onStatus = () => {},
  onBusy = () => {},
}) {
  let stateRevision = 0;
  let refreshController = null;
  let actionPending = false;

  async function refresh() {
    if (actionPending) return null;
    const revision = ++stateRevision;
    refreshController?.abort();
    refreshController = null;
    const currentProject = projectId().trim();
    if (!currentProject) {
      onStatus("Enter a project ID to load live state.");
      return null;
    }
    const controller = new AbortController();
    refreshController = controller;
    onStatus("Loading live state...", "pending");
    try {
      const response = await fetcher(
        `/projects/${encodeURIComponent(currentProject)}/dashboard`,
        { cache: "no-store", signal: controller.signal },
      );
      const payload = await readJson(response);
      if (revision !== stateRevision) return null;
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
      const response = await fetcher(
        `/projects/${encodeURIComponent(projectId().trim())}/actions`,
        {
          method: "POST",
          headers,
          body: JSON.stringify({ ...payload, approved: true }),
        },
      );
      const result = await readJson(response);
      if (revision !== stateRevision) return null;
      if (result.state) onState(result.state);
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
      onBusy(false);
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
