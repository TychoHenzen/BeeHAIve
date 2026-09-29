const board = document.querySelector("#board");
const statusText = document.querySelector("#snapshot-status");

function text(value) {
  return value == null ? "" : String(value);
}

function render(snapshot) {
  board.replaceChildren();
  for (const column of snapshot.columns ?? []) {
    const columnElement = document.createElement("section");
    columnElement.className = "column";
    const heading = document.createElement("h3");
    heading.textContent = text(column.status);
    columnElement.append(heading);
    for (const item of column.items ?? []) {
      const card = document.createElement("article");
      card.className = "card";
      const icon = document.createElement("span");
      icon.className = "type-icon";
      icon.setAttribute("aria-label", text(item.type));
      icon.textContent = { Issue: "●", PullRequest: "↗", DraftIssue: "✎" }[item.type] ?? "•";
      const meta = document.createElement("p");
      meta.className = "card-meta";
      meta.append(icon, document.createTextNode(` ${text(item.type)} · ${text(item.repository)}#${text(item.number)}`));
      const title = document.createElement(item.url ? "a" : "h4");
      title.textContent = text(item.title);
      if (item.url) {
        title.href = item.url;
        title.target = "_blank";
        title.rel = "noreferrer";
      }
      const labels = document.createElement("p");
      labels.className = "labels";
      labels.textContent = (item.labels ?? []).join(" · ");
      const holder = document.createElement("p");
      holder.className = "card-holder";
      holder.textContent = "held by agent X";
      card.append(meta, title, labels, holder);
      columnElement.append(card);
    }
    board.append(columnElement);
  }
  const limited = snapshot.rate_limited_until
    ? ` · GitHub rate limited until ${new Date(snapshot.rate_limited_until).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
    : "";
  statusText.textContent = `Snapshot ${text(snapshot.fetched_at) || "not fetched"}${limited}`;
}

async function load(path = "/api/project", method = "GET") {
  statusText.textContent = "Loading the linked Project snapshot…";
  const response = await fetch(path, { method });
  if (!response.ok) {
    throw new Error(`Project request failed: ${response.status}`);
  }
  render(await response.json());
}

document.querySelector("#refresh").addEventListener("click", async () => {
  try {
    await load("/api/project/refresh", "POST");
  } catch (error) {
    statusText.textContent = error.message;
  }
});

for (const tab of document.querySelectorAll("[data-tab]")) {
  tab.addEventListener("click", () => {
    for (const candidate of document.querySelectorAll("[data-tab]")) {
      candidate.classList.toggle("active", candidate === tab);
    }
    for (const panel of document.querySelectorAll("[data-panel]")) {
      panel.classList.toggle("active", panel.dataset.panel === tab.dataset.tab);
    }
  });
}

load().catch((error) => {
  statusText.textContent = error.message;
});
