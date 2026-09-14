export function createRepositoryRenderer(dom, renderPbi, runAction) {
  const { element } = dom;
  function renderRepository(repository) {
    const section = element("section", undefined, "repo");
    const header = element("div", undefined, "repo-header");
    const name = element("h2", repository.name || "Unknown repository");
    name.append(element("span", repository.active ? " active" : " inactive", "muted"));
    const writer = repository.writer || { status: "idle" };
    const writerText = writer.status === "active"
      ? `Writer active on PBI #${writer.current_pbi}`
      : "Writer idle";
    header.append(name, element("div", writerText, `writer ${writer.status}`));
    section.append(header);
    if (repository.active && writer.status !== "active") {
      const controls = element("div", undefined, "actions");
      const start = element("button", "Start writer", "secondary");
      start.type = "button";
      start.addEventListener("click", () => runAction({ action: "start", repository: repository.name }));
      controls.append(start);
      section.append(controls);
    }
    const body = element("div", undefined, "repo-body");
    const pbis = repository.pbis || [];
    if (pbis.length === 0) body.append(element("div", "No PBIs in this repository.", "empty"));
    pbis.forEach((pbi) => body.append(renderPbi(repository.name, pbi)));
    section.append(body);
    return section;
  }
  return renderRepository;
}
