/*
 * React consumers can use the same live projection as the built-in dashboard.
 * The repository runtime uses dashboard.html and dashboard.js, which have no
 * frontend build dependency.
 */
export async function loadDashboardState(projectId, fetcher = fetch) {
  const response = await fetcher(
    `/projects/${encodeURIComponent(projectId)}/dashboard`,
    { cache: "no-store" },
  );
  if (!response.ok) {
    throw new Error(`Dashboard request failed: ${response.status}`);
  }
  return response.json();
}

export function SwarmDashboard({ state }) {
  return <pre>{JSON.stringify(state, null, 2)}</pre>;
}
