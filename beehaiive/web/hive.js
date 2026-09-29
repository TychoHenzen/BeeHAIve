const dom = typeof document === "undefined" ? null : document;
const map = dom?.querySelector("#hive-map");
const details = dom?.querySelector("#hive-details");
const status = dom?.querySelector("#hive-status");
const alertList = dom?.querySelector("#hive-alert-list");
const refreshButton = dom?.querySelector("#hive-refresh");
const motionButton = dom?.querySelector("#hive-motion");

let snapshot = null;
let cursor = "";
let pollHandle = null;
let forcedReducedMotion = null;
const previousPositions = new Map();

export function interpolatePosition(start, end, progress) {
  const amount = Math.max(0, Math.min(1, Number(progress)));
  return {
    x: Number(start.x) + (Number(end.x) - Number(start.x)) * amount,
    y: Number(start.y) + (Number(end.y) - Number(start.y)) * amount,
  };
}

function reducedMotion() {
  if (forcedReducedMotion !== null) return forcedReducedMotion;
  return typeof window.matchMedia === "function"
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function text(value) {
  return value === null || value === undefined ? "" : String(value);
}

async function request(path, options = {}) {
  const response = await fetch(path, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Hive request failed: ${response.status}`);
  return payload;
}

function svgElement(name, attributes = {}) {
  const element = dom.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [attribute, value] of Object.entries(attributes)) element.setAttribute(attribute, text(value));
  return element;
}

export function pointFor(region, stationId, regionOffset) {
  const station = (region.stations || []).find((candidate) => candidate.id === stationId);
  const layout = station?.layout || region.layout?.[stationId] || { x: 0, y: 0 };
  return { x: Number(layout.x || 0) + 110, y: Number(layout.y || 0) + regionOffset + 46 };
}

export function regionSize(region) {
  const stations = region.stations || [];
  return {
    width: Math.max(780, ...stations.map((station) => Number(station.layout?.x || 0) + 260)),
    height: Math.max(160, ...stations.map((station) => Number(station.layout?.y || 0) + 120)),
  };
}

function animateUnit(element, start, end) {
  if (reducedMotion() || !start || !end || typeof requestAnimationFrame !== "function") {
    element.setAttribute("transform", `translate(${end.x} ${end.y})`);
    return;
  }
  const started = typeof performance !== "undefined" ? performance.now() : Date.now();
  const frame = (now) => {
    const elapsed = Math.min(1, ((now - started) / 1000));
    const point = interpolatePosition(start, end, elapsed);
    element.setAttribute("transform", `translate(${point.x} ${point.y})`);
    if (elapsed < 1) requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}

function renderMap(value) {
  map.replaceChildren();
  const regions = value.regions || [];
  const units = value.units || [];
  const regionOffsets = new Map();
  let height = 30;
  let width = 780;
  for (const region of regions) {
    regionOffsets.set(`${region.workflow_id}:${region.revision}`, height);
    const size = regionSize(region);
    width = Math.max(width, size.width);
    height += size.height + 58;
  }
  const svg = svgElement("svg", { viewBox: `0 0 ${width} ${Math.max(height, 220)}`, role: "img" });
  for (const region of regions) {
    const offset = regionOffsets.get(`${region.workflow_id}:${region.revision}`) || 30;
    const group = svgElement("g");
    const heading = svgElement("text", { x: 18, y: offset - 12, class: "hive-region-label" });
    heading.textContent = `${text(region.name)} · workflow ${text(region.workflow_id)} r${text(region.revision)}`;
    group.append(heading);
    const points = new Map();
    for (const station of region.stations || []) points.set(station.id, pointFor(region, station.id, offset));
    for (const road of region.roads || []) {
      const from = points.get(road.from);
      const to = points.get(road.to);
      if (!from || !to) continue;
      const line = svgElement("line", { x1: from.x, y1: from.y, x2: to.x, y2: to.y, class: "hive-road" });
      group.append(line);
      const label = svgElement("text", { x: (from.x + to.x) / 2, y: (from.y + to.y) / 2 - 5, class: "hive-road-label" });
      label.textContent = text(road.outcome);
      group.append(label);
    }
    for (const station of region.stations || []) {
      const point = points.get(station.id);
      if (!point) continue;
      const polygon = svgElement("polygon", {
        points: `${point.x - 96},${point.y - 30} ${point.x - 76},${point.y - 48} ${point.x + 76},${point.y - 48} ${point.x + 96},${point.y - 30} ${point.x + 76},${point.y + 30} ${point.x - 76},${point.y + 30}`,
        class: "hive-station",
        "data-station-id": station.id,
        "data-depot": station.depot,
        "data-terminal": station.terminal,
        tabindex: 0,
      });
      polygon.addEventListener("click", () => showStationDetails(region, station));
      polygon.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") showStationDetails(region, station); });
      group.append(polygon);
      const title = svgElement("text", { x: point.x, y: point.y - 7, "text-anchor": "middle" });
      title.textContent = text(station.title);
      group.append(title);
      const badge = svgElement("text", { x: point.x, y: point.y + 10, "text-anchor": "middle", class: "hive-station-badge" });
      badge.textContent = `${text(station.agents_present)} agents · ${text(station.steps_completed_24h)} steps`;
      group.append(badge);
      if (station.waiting_items) {
        const waiting = svgElement("text", { x: point.x, y: point.y + 25, "text-anchor": "middle", class: "hive-station-badge" });
        waiting.textContent = `${text(station.waiting_items)} waiting`;
        group.append(waiting);
      }
    }
    svg.append(group);
  }
  const seen = new Set();
  for (const unit of units) {
    const region = regions.find((candidate) => candidate.workflow_id === unit.region && candidate.revision === unit.workflow_revision);
    if (!region) continue;
    const offset = regionOffsets.get(`${region.workflow_id}:${region.revision}`) || 30;
    const destination = pointFor(region, unit.station, offset);
    const number = seen.has(`${unit.region}:${unit.station}`) ? 1 : 0;
    seen.add(`${unit.region}:${unit.station}`);
    destination.x += unit.parked ? -90 : -70 + number * 28;
    destination.y += unit.parked ? 60 : 74;
    const group = svgElement("g", { class: "hive-unit", "data-agent-id": unit.agent_id, "data-status": unit.status, tabindex: 0 });
    group.addEventListener("click", () => showUnitDetails(unit));
    group.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") showUnitDetails(unit); });
    const circle = svgElement("circle", { r: 16, fill: unit.color || "#e3b341" });
    group.append(circle);
    if (unit.progress !== null && unit.progress !== undefined) {
      const ring = svgElement("circle", {
        class: "hive-unit-progress",
        r: 21,
        fill: "none",
        "stroke-dasharray": "131.95",
        "stroke-dashoffset": `${131.95 * (1 - Number(unit.progress))}`,
      });
      group.append(ring);
    }
    if (unit.stalled) {
      const marker = svgElement("text", { y: -25, "text-anchor": "middle", class: "hive-unit-alert" });
      marker.textContent = "!";
      group.append(marker);
    }
    const label = svgElement("text", { y: 31, "text-anchor": "middle", class: "hive-unit-label" });
    label.textContent = text(unit.name);
    group.append(label);
    const item = unit.item?.repository && unit.item?.number ? svgElement("text", { y: 44, "text-anchor": "middle", class: "hive-unit-label" }) : null;
    if (item) {
      item.textContent = `${text(unit.item.repository)}#${text(unit.item.number)}`;
      group.append(item);
    }
    svg.append(group);
    animateUnit(group, previousPositions.get(unit.agent_id), destination);
    previousPositions.set(unit.agent_id, destination);
  }
  map.append(svg);
}

function appendField(container, label, value) {
  const row = dom.createElement("div");
  const term = dom.createElement("dt");
  term.textContent = label;
  const detail = dom.createElement("dd");
  detail.textContent = value;
  row.append(term, detail);
  container.append(row);
}

function showStationDetails(region, station) {
  details.replaceChildren();
  const heading = dom.createElement("h3");
  heading.textContent = station.title;
  details.append(heading);
  const list = dom.createElement("dl");
  list.className = "hive-detail-list";
  appendField(list, "Workflow", `${text(region.workflow_id)} revision ${text(region.revision)}`);
  appendField(list, "State", `${text(station.id)} · ${text(station.action)}`);
  appendField(list, "Prompt", text(station.prompt) || "No state prompt");
  appendField(list, "Skill", text(station.skill) || "No skill");
  appendField(list, "Outcomes", (station.outcomes || []).join(", ") || "always");
  appendField(list, "Agents", `${text(station.agents_present)} present`);
  appendField(list, "Recent steps", `${text(station.steps_completed_24h)} completed in 24h`);
  appendField(list, "Typical duration", station.duration?.typical_seconds ? `${station.duration.typical_seconds.toFixed(1)} seconds` : "no history");
  appendField(list, "Recent outcomes", (station.recent_steps || []).map((step) => text(step.outcome) || "running").join(", ") || "none");
  details.append(list);
}

async function showUnitDetails(unit) {
  details.replaceChildren();
  const heading = dom.createElement("h3");
  heading.textContent = `${text(unit.name)} · ${text(unit.status)}`;
  details.append(heading);
  const list = dom.createElement("dl");
  list.className = "hive-detail-list";
  appendField(list, "Station", text(unit.station));
  appendField(list, "Workflow", `${text(unit.workflow_id)} revision ${text(unit.workflow_revision)}`);
  appendField(list, "Pass", text(unit.pass_id) || "No active pass");
  appendField(list, "Item", unit.item?.repository && unit.item?.number ? `${unit.item.repository}#${unit.item.number}` : "No item");
  appendField(list, "Timing", unit.timing?.typical_seconds ? `${unit.timing.typical_seconds.toFixed(1)} seconds typical` : "no history");
  appendField(list, "Progress", unit.elapsed_seconds === null || unit.elapsed_seconds === undefined ? "Not running" : `${Math.round(unit.elapsed_seconds)} seconds elapsed${unit.overdue ? " · overdue" : ""}`);
  details.append(list);
  if (unit.status !== "stopped") {
    const stop = dom.createElement("button");
    stop.type = "button";
    stop.textContent = "Stop agent";
    stop.addEventListener("click", async () => {
      await request(`/api/agents/${encodeURIComponent(unit.agent_id)}/stop`, { method: "POST" });
      await loadHive();
    });
    details.append(stop);
  }
  const steps = dom.createElement("h4");
  steps.textContent = "Pass steps";
  details.append(steps);
  const stepList = dom.createElement("ul");
  for (const step of unit.steps || []) {
    const item = dom.createElement("li");
    item.textContent = `${text(step.state_id)} · ${text(step.status)} · ${text(step.outcome) || "running"} · ${text(step.summary)}`;
    stepList.append(item);
  }
  details.append(stepList);
  if (unit.pass_id) {
    try {
      const logs = await request(`/api/agents/${encodeURIComponent(unit.agent_id)}/logs?pass_id=${encodeURIComponent(unit.pass_id)}`);
      const log = dom.createElement("pre");
      log.className = "hive-log";
      log.textContent = (logs.lines || []).join("\n") || "No log lines yet.";
      details.append(log);
    } catch (error) {
      const failure = dom.createElement("p");
      failure.textContent = error.message;
      details.append(failure);
    }
  }
}

function renderAlerts(alerts) {
  alertList.replaceChildren();
  if (!alerts.length) {
    alertList.textContent = "No active alerts.";
    return;
  }
  for (const alert of alerts) {
    const card = dom.createElement("article");
    card.className = "hive-alert-card";
    const body = dom.createElement("div");
    const message = dom.createElement("p");
    message.textContent = `${text(alert.agent_name)} · ${text(alert.station_title)} · ${text(alert.kind)}`;
    const reason = dom.createElement("small");
    reason.textContent = text(alert.message);
    body.append(message, reason);
    const acknowledge = dom.createElement("button");
    acknowledge.type = "button";
    acknowledge.textContent = "Acknowledge";
    acknowledge.addEventListener("click", async () => {
      if (!alert.pass_id) return;
      await request(`/api/passes/${encodeURIComponent(alert.pass_id)}/acknowledge`, { method: "POST", body: JSON.stringify({ acknowledged_by: "operator" }), headers: { "Content-Type": "application/json" } });
      await loadHive();
    });
    card.append(body, acknowledge);
    alertList.append(card);
  }
}

async function loadHive() {
  try {
    const value = await request("/api/hive");
    snapshot = value;
    renderMap(value);
    renderAlerts(value.alerts || []);
    status.textContent = `Updated ${new Date(value.generated_at).toLocaleTimeString()} · ${text((value.units || []).length)} agents`;
  } catch (error) {
    status.textContent = error.message;
  }
}

async function pollEvents() {
  try {
    const value = await request(`/api/hive/events?since=${encodeURIComponent(cursor)}`);
    cursor = value.cursor || cursor;
    if ((value.events || []).length) await loadHive();
    else if (snapshot) renderMap(snapshot);
  } catch (error) {
    status.textContent = error.message;
  }
  pollHandle = setTimeout(pollEvents, 2000);
}

function updateMotionButton() {
  const label = forcedReducedMotion === null ? "system" : forcedReducedMotion ? "on" : "off";
  motionButton.textContent = `Reduced motion: ${label}`;
}

function initHive() {
  if (!map || !details || !status || !alertList) return;
  refreshButton?.addEventListener("click", () => loadHive());
  motionButton?.addEventListener("click", () => {
    forcedReducedMotion = forcedReducedMotion === null ? true : forcedReducedMotion ? false : null;
    updateMotionButton();
    if (snapshot) renderMap(snapshot);
  });
  updateMotionButton();
  loadHive();
  if (pollHandle === null) pollHandle = setTimeout(pollEvents, 2000);
}

if (typeof document !== "undefined") initHive();
