export function createDashboardClient({
  fetcher,
  projectId,
  apiKey,
  saveApiKey = () => {},
  onState = () => {},
  onStatus = () => {},
  onBusy = () => {},
}) {
  let refreshSequence = 0;
  let refreshController = null;
  let actionPending = false;

  async function refresh() {
    const currentProject = projectId().trim();
    if (!currentProject) {
      onStatus("Enter a project ID to load live state.");
      return null;
    }
    const sequence = ++refreshSequence;
    refreshController?.abort();
    refreshController = new AbortController();
    onStatus("Loading live state...", "pending");
    try {
      const response = await fetcher(
        `/projects/${encodeURIComponent(currentProject)}/dashboard`,
        { cache: "no-store", signal: refreshController.signal },
      );
      const payload = await readJson(response);
      if (sequence !== refreshSequence) return null;
      onState(payload);
      onStatus(`Updated ${payload.updated_at || "now"}.`, "success");
      return payload;
    } catch (error) {
      if (error.name === "AbortError" || sequence !== refreshSequence) return null;
      onStatus(`Live state failed: ${error.message}`, "failure");
      return null;
    }
  }

  async function runAction(payload) {
    if (actionPending) return null;
    actionPending = true;
    onBusy(true);
    onStatus(`${payload.action} pending...`, "pending");
    const headers = { "Content-Type": "application/json" };
    const configuredApiKey = apiKey();
    if (configuredApiKey) {
      headers["X-API-Key"] = configuredApiKey;
      saveApiKey(configuredApiKey);
    }
    try {
      const response = await fetcher(
        `/projects/${encodeURIComponent(projectId().trim())}/actions`,
        {
          method: "POST",
          headers,
          body: JSON.stringify({ ...payload, approved: true }),
        },
      );
      const result = await readJson(response);
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
