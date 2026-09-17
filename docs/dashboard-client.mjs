export function createDashboardClient({
  fetcher,
  projectId,
  workflowId = () => "",
  archived = () => false,
  apiKey = () => "",
  saveApiKey = () => {},
  onState = () => {},
  onStatus = () => {},
  onBusy = () => {},
}) {
  let stateRevision = 0;
  let refreshController = null;
  let actionPending = false;
  let actionProject = "";
  let actionWorkflow = "";
  let currentState = null;
  let stateProject = "";
  let stateWorkflow = "";

  async function refresh() {
    const currentProject = projectId().trim();
    const currentWorkflow = workflowId().trim();
    if (actionPending) {
      if (
        currentProject !== actionProject
        || currentWorkflow !== actionWorkflow
      ) {
        stateRevision += 1;
      }
      return null;
    }
    const revision = ++stateRevision;
    refreshController?.abort();
    refreshController = null;
    if (
      (stateProject && stateProject !== currentProject)
      || stateWorkflow !== currentWorkflow
    ) {
      currentState = null;
      stateProject = currentProject;
      stateWorkflow = currentWorkflow;
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
      const query = new URLSearchParams();
      if (archived()) query.set("archived", "true");
      if (currentWorkflow) query.set("workflow_id", currentWorkflow);
      const queryString = query.toString();
      const response = await fetcher(
        `/projects/${encodeURIComponent(currentProject)}/dashboard${queryString ? `?${queryString}` : ""}`,
        { cache: "no-store", signal: controller.signal },
      );
      const payload = await readJson(response);
      if (revision !== stateRevision) return null;
      currentState = payload;
      stateProject = currentProject;
      stateWorkflow = currentWorkflow;
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
    actionWorkflow = workflowId().trim();
    const revision = ++stateRevision;
    refreshController?.abort();
    refreshController = null;
    onBusy(true);
    onStatus(`${payload.action} pending...`, "pending");
    const headers = {
      "Content-Type": "application/json",
      "X-BeeHAIve-Dashboard": "1",
    };
    const configuredApiKey = apiKey();
    try {
      if (configuredApiKey) {
        headers["X-API-Key"] = configuredApiKey;
        saveApiKey(configuredApiKey);
      }
      const query = new URLSearchParams();
      if (archived()) query.set("archived", "true");
      if (actionWorkflow) query.set("workflow_id", actionWorkflow);
      const queryString = query.toString();
      const response = await fetcher(
        `/projects/${encodeURIComponent(projectId().trim())}/actions${queryString ? `?${queryString}` : ""}`,
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
        stateWorkflow = actionWorkflow;
        onState(result.state);
      } else if (currentState) {
        currentState = {
          ...currentState,
          actions: [...(currentState.actions || []), result.action].slice(-50),
        };
        stateWorkflow = actionWorkflow;
        onState(currentState);
      } else {
        currentState = {
          project_id: actionProject,
          repositories: [],
          counts: {},
          actions: [result.action],
        };
        stateProject = actionProject;
        stateWorkflow = actionWorkflow;
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
      const changedWorkflow = workflowId().trim() !== actionWorkflow;
      actionProject = "";
      actionWorkflow = "";
      onBusy(false);
      if (changedProject || changedWorkflow) void refresh();
    }
  }

  async function runAutonomous(payload) {
    if (actionPending) return null;
    actionPending = true;
    actionProject = projectId().trim();
    actionWorkflow = workflowId().trim();
    stateRevision += 1;
    refreshController?.abort();
    refreshController = null;
    onBusy(true);
    onStatus("autonomous lifecycle pending...", "pending");
    try {
      const response = await fetcher(
        `/projects/${encodeURIComponent(actionProject)}/autonomous-runs`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-BeeHAIve-Dashboard": "1",
          },
          body: JSON.stringify({ ...payload, approved: true }),
        },
      );
      const result = await readJson(response);
      if (result.status === "running" && result.run_id) {
        onStatus("autonomous lifecycle running on server...", "pending");
        return await waitForAutonomous(result, actionProject);
      }
      onStatus(`autonomous lifecycle ${result.status || "started"}.`, "success");
      return result;
    } catch (error) {
      onStatus(`autonomous lifecycle failed: ${error.message}`, "failure");
      return null;
    } finally {
      actionPending = false;
      actionProject = "";
      actionWorkflow = "";
      onBusy(false);
      void refresh();
    }
  }

  async function waitForAutonomous(started, project) {
    for (let attempt = 0; attempt < 60; attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
      try {
        const response = await fetcher(
          `/projects/${encodeURIComponent(project)}/autonomous-runs/${encodeURIComponent(started.run_id)}`,
          { cache: "no-store" },
        );
        const status = await readJson(response);
        if (status.status !== "running") {
          onStatus(`autonomous lifecycle ${status.status || "finished"}.`, status.status === "completed" ? "success" : "failure");
          return status;
        }
        onStatus(`autonomous lifecycle running on server${status.current_step ? ` · ${status.current_step}` : "..."}`, "pending");
      } catch (error) {
        onStatus(`autonomous lifecycle status unavailable: ${error.message}`, "failure");
        return started;
      }
    }
    onStatus("autonomous lifecycle is still running on the server. Inspect Agents for its state.", "pending");
    return started;
  }

  async function configureScheduler(config) {
    if (actionPending) return null;
    actionPending = true;
    actionProject = projectId().trim();
    actionWorkflow = workflowId().trim();
    refreshController?.abort();
    refreshController = null;
    onBusy(true);
    onStatus("scheduler configuration pending...", "pending");
    try {
      const response = await fetcher(
        `/projects/${encodeURIComponent(actionProject)}/scheduler`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-BeeHAIve-Dashboard": "1",
          },
          body: JSON.stringify({ ...config, approved: true }),
        },
      );
      const result = await readJson(response);
      if (currentState && result.scheduler) {
        currentState = {
          ...currentState,
          scheduler: result.scheduler,
          actions: [...(currentState.actions || []), result.action].slice(-50),
        };
        onState(currentState);
      }
      onStatus("scheduler settings applied.", "success");
      return result;
    } catch (error) {
      const message = `scheduler configuration failed: ${error.message}`;
      onStatus(message, "failure");
      return { error: message };
    } finally {
      actionPending = false;
      actionProject = "";
      actionWorkflow = "";
      onBusy(false);
      void refresh();
    }
  }

  return { refresh, runAction, runAutonomous, configureScheduler };
}

async function readJson(response) {
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.detail || `Request failed (${response.status})`);
  }
  return payload;
}
