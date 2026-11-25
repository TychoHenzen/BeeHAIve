// Where every open issue of a repository stands, and whether each queue can
// move it, from archon-home's ledger. A reading takes a GitHub snapshot and
// most of a minute, so the page shows the server's last reading (kept for
// five minutes) and reads again only on load without one, or when asked.
import { actionButton, ago, el, notice, replace, section, short } from "../dom.js";
import { githubUrl, pipelineCounts, queueProblems, reasonHead, stuckGroups } from "../model.js";

const STATE_ORDER = ["asking", "working", "queued"];
const HEADINGS = {
  asking: "Waiting on your answer",
  working: "Being worked",
  queued: "Waiting in a queue",
};

function pill(state) {
  return el("span", { class: `pill tone-${state}` }, state);
}

// A reason, with a long one cut to its opening and the rest behind a toggle.
function reason(tag, attrs, text) {
  const head = reasonHead(text);
  return head ? el(tag, attrs, head, el("details", {}, el("summary", {}, "Full reason"), el("pre", { class: "inline" }, text))) : el(tag, attrs, text);
}

function runLink(id) {
  return id ? el("a", { href: `#/runs/${id}` }, el("code", {}, short(id))) : null;
}

function issueRef(repository, row) {
  return el(
    "span",
    { class: "issue-ref" },
    el("a", { href: githubUrl(repository, "issue", row.issue), target: "_blank", rel: "noreferrer" }, `#${row.issue}`),
    " ",
    row.title,
    row.pull ? [" (", el("a", { href: githubUrl(repository, "pull", row.pull), target: "_blank", rel: "noreferrer" }, `PR #${row.pull}`), ")"] : null,
  );
}

function queueList(queues) {
  return el(
    "ul",
    { class: "ledger" },
    queues.map((queue) =>
      el("li", {}, pill(queue.state), el("div", {}, el("strong", {}, queue.queue), " ", runLink(queue.run), el("div", { class: "why" }, queue.detail))),
    ),
  );
}

function stuckList(repository, issues) {
  const groups = stuckGroups(issues);
  if (groups.length === 0) return notice("Nothing is stuck.", "ok");
  return groups.map((group) =>
    el(
      "div",
      { class: "cause" },
      reason("div", { class: "cause-why" }, group.rows.length > 1 ? `${group.rows.length} issues: ${group.detail}` : group.detail),
      el(
        "ul",
        { class: "issue-refs" },
        group.rows.map((row) => el("li", {}, issueRef(repository, row), " ", runLink(row.run))),
      ),
    ),
  );
}

function stateList(repository, rows) {
  return el(
    "ul",
    { class: "ledger" },
    rows.map((row) => el("li", {}, pill(row.state), el("div", {}, issueRef(repository, row), " ", runLink(row.run), reason("div", { class: "why" }, row.detail)))),
  );
}

function render(ledger) {
  const repository = ledger.repository;
  const issues = ledger.issues ?? [];
  const broken = queueProblems(ledger.queues);
  const pulls = ledger.pulls ?? [];
  return [
    el(
      "p",
      { class: "summary" },
      el("strong", {}, pipelineCounts(ledger.counts)),
      broken.length ? el("span", { class: "tone-stuck" }, ` - ${broken.length === 1 ? "1 queue cannot" : `${broken.length} queues cannot`} run`) : null,
      el("span", { class: "muted small" }, " - read ", ago(ledger.at)),
    ),
    section("Queues", queueList(ledger.queues ?? [])),
    section("Stuck", stuckList(repository, issues)),
    STATE_ORDER.map((state) => {
      const rows = issues.filter((row) => row.state === state);
      return rows.length ? section(HEADINGS[state], stateList(repository, rows)) : null;
    }),
    pulls.length
      ? section(
          "Pull requests no issue owns",
          el(
            "ul",
            { class: "ledger" },
            pulls.map((pull) =>
              el(
                "li",
                {},
                pill("stuck"),
                el(
                  "div",
                  {},
                  el("a", { href: githubUrl(repository, "pull", pull.number), target: "_blank", rel: "noreferrer" }, `PR #${pull.number}`),
                  " ",
                  el("code", {}, pull.headRef),
                  el("div", { class: "why" }, pull.detail),
                ),
              ),
            ),
          ),
        )
      : null,
  ];
}

export default {
  id: "pipeline",
  title: "Pipeline",
  nav: true,
  match: (path) => (path === "/pipeline" ? {} : null),
  mount(root, params, ctx) {
    const bar = el("div", { class: "row pipeline-bar" });
    const body = el("div", { "aria-live": "polite" });
    replace(
      root,
      el(
        "div",
        { class: "page-head" },
        el("h1", {}, "Pipeline"),
        el("p", { class: "muted" }, "Where every open issue stands. Stuck means no run, queue or question will move it until someone acts."),
      ),
      bar,
      body,
    );
    let alive = true;
    let repository = null;

    async function read() {
      replace(body, notice("Reading GitHub and Archon. This takes about a minute."));
      try {
        const answer = await ctx.api.readPipeline(repository);
        if (alive) replace(body, render(answer.ledger));
      } catch (error) {
        if (alive) replace(body, notice(`The pipeline could not be read: ${error.message}`, "error"));
      }
    }

    function showBar(repositories) {
      const picker =
        repositories.length > 1
          ? el(
              "select",
              { "aria-label": "Repository", onchange: (event) => ctx.navigate(`/pipeline?repo=${encodeURIComponent(event.target.value)}`) },
              repositories.map((name) => el("option", { value: name, selected: name === repository }, name)),
            )
          : el("strong", {}, repository);
      replace(bar, picker, actionButton("Read again", read));
    }

    ctx.api
      .pipeline()
      .then(async ({ repositories }) => {
        if (!alive) return;
        if (repositories.length === 0) {
          replace(body, notice("No pbi queue binding names a repository yet. Schedule a queue on the Triggers page."));
          return;
        }
        repository = repositories.includes(params.repo) ? params.repo : repositories[0];
        showBar(repositories);
        const kept = await ctx.api.pipeline(repository);
        if (!alive) return;
        if (kept.ledger) replace(body, render(kept.ledger));
        else await read();
      })
      .catch((error) => {
        if (alive) replace(body, notice(`BeeHAIve did not answer: ${error.message}`, "error"));
      });
    return () => {
      alive = false;
    };
  },
};
