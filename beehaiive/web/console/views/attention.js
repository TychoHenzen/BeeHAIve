// Every paused run across the install that is waiting on a person.
import { attentionPanel } from "../attention-panel.js";
import { ago, el, notice, replace, short } from "../dom.js";

export default {
  id: "attention",
  title: "Attention",
  nav: true,
  match: (path) => (path === "/attention" ? {} : null),
  mount(root, _params, ctx) {
    const list = el("div", { class: "attention-queue" });
    replace(
      root,
      el("div", { class: "page-head" }, el("h1", {}, "Attention"), el("p", { class: "muted" }, "Runs paused until someone answers. The same queue as archon workflow attention --all.")),
      list,
    );

    // One card per pause, kept across refreshes so typed answers survive.
    let cards = new Map();

    async function load() {
      try {
        const queue = await ctx.api.attention();
        const items = queue.items ?? [];
        const degraded = queue.degraded ?? [];
        const next = new Map();
        for (const item of items) {
          const key = JSON.stringify([item.runId, item.occurrence ?? item.kind]);
          next.set(key, cards.get(key) ?? card(item));
        }
        cards = next;
        replace(
          list,
          items.length ? null : notice("Nothing is waiting on you."),
          [...cards.values()],
          degraded.length ? notice(`${degraded.length} run(s) could not be read.`, "error") : null,
        );
      } catch (error) {
        replace(list, notice(`Archon did not answer: ${error.message}`, "error"));
      }
    }

    function card(item) {
      return el(
        "article",
        { class: "panel" },
        el(
          "header",
          { class: "item-head" },
          el("a", { href: `#/runs/${item.runId}` }, el("strong", {}, item.workflow ?? "run"), " ", el("code", {}, short(item.runId))),
          el("span", { class: "muted" }, item.project?.key ?? ""),
          el("span", { class: "muted small" }, "paused ", ago(item.pausedAt)),
        ),
        attentionPanel(item.runId, item, null, ctx, () => (load(), ctx.refreshAttention())),
      );
    }

    load();
    return ctx.live.subscribe(load);
  },
};
