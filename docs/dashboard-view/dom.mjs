export function createDom(document) {
  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }

  function renderEvidenceLink(label, value) {
    const raw = String(value);
    if (!/^https?:\/\/[^\s]+$/i.test(raw)) {
      return element("div", `${label}: ${raw}`, "muted");
    }
    const container = element("div", undefined, "muted");
    const link = element("a", raw);
    link.href = raw;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    container.append(element("span", `${label}: `), link);
    return container;
  }

  function renderListSection(title, values, format) {
    const section = element("section");
    section.append(element("h3", title));
    const list = values || [];
    if (list.length === 0) section.append(element("div", "None", "muted"));
    else {
      const items = element("ul");
      list.forEach((value) => items.append(element("li", format(value), "muted")));
      section.append(items);
    }
    return section;
  }

  function pullRequestLabel(item) {
    const evidence = [
      typeof item.url === "string" ? item.url : "",
      item.source_branch
        ? `branch ${item.source_branch} (${item.source_branch_state || "unknown"})`
        : "",
    ].filter(Boolean);
    const suffix = evidence.length ? ` (${evidence.join(", ")})` : "";
    if (item.merged === true) return `#${item.number}: merged${suffix}`;
    const state = typeof item.state === "string" ? item.state.toLowerCase() : "";
    const decision = item.review_decision || "";
    if (state === "open" && !decision) return `#${item.number}: open, review pending${suffix}`;
    if (state && decision) return `#${item.number}: ${state}, ${decision}${suffix}`;
    return `#${item.number}: ${state || decision || "review pending"}${suffix}`;
  }

  function checkStatusClass(verdict) {
    if (verdict === "passing") return "status success";
    if (verdict === "blocking") return "status failure";
    return "status pending";
  }
  return { element, renderEvidenceLink, renderListSection, pullRequestLabel, checkStatusClass };
}
