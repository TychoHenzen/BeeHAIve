// Every paused run across the install that is waiting on a person. Questions
// that offer the same choices are grouped, so one tap answers all of them.
import { attentionPanel, choiceButtons } from "../attention-panel.js";
import { ago, el, notice, replace, short } from "../dom.js";
import { answerBody, pauseKey, questionGroups, questionParts, writesAnswer } from "../model.js";

export default {
  id: "attention",
  title: "Attention",
  nav: true,
  match: (path) => (path === "/attention" ? {} : null),
  mount(root, _params, ctx) {
    const list = el("div", { class: "attention-queue" });
    replace(
      root,
      el(
        "div",
        { class: "page-head" },
        el("h1", {}, "Attention"),
        el("p", { class: "muted" }, "Runs paused until someone answers. Questions that offer the same choices are grouped, so one tap answers them all."),
      ),
      list,
    );

    // One card per pause or group, kept across refreshes. Each run's answer
    // panel is kept on its own, by run and pause, so a run that joins or
    // leaves a group keeps whatever was typed into it.
    let cards = new Map();
    let panels = new Map();
    const pauseOf = (item) => JSON.stringify([item.runId, pauseKey(item)]);
    const panelOf = (item) => panels.get(pauseOf(item));
    const refresh = () => (load(), ctx.refreshAttention());

    async function load() {
      try {
        const queue = await ctx.api.attention();
        const items = queue.items ?? [];
        const degraded = queue.degraded ?? [];
        panels = new Map(items.map((item) => [pauseOf(item), panelOf(item) ?? attentionPanel(item.runId, item, null, ctx, refresh)]));
        const next = new Map();
        for (const group of questionGroups(items)) {
          const key = JSON.stringify(group.items.map(pauseOf));
          next.set(key, cards.get(key) ?? (group.items.length > 1 ? groupCard(group.items) : card(group.items[0])));
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

    function runLink(item) {
      return el("a", { href: `#/runs/${item.runId}` }, el("strong", {}, item.workflow ?? "run"), " ", el("code", {}, short(item.runId)));
    }

    function card(item) {
      return el(
        "article",
        { class: "panel" },
        el(
          "header",
          { class: "item-head" },
          runLink(item),
          el("span", { class: "muted" }, item.project?.key ?? ""),
          el("span", { class: "muted small" }, "paused ", ago(item.pausedAt)),
        ),
        panelOf(item),
      );
    }

    // Several runs asking the same question: the shared choices answer every
    // one of them, each with its own wording of the choice. Each run stays
    // open below for an answer of its own.
    function groupCard(items) {
      const parts = items.map((item) => questionParts(item.message));
      const agreed = parts.every((part) => part.recommended === parts[0].recommended) ? parts[0].recommended : null;
      // A choice that needs the owner's own words is answered run by run.
      const oneTap = parts[0].choices.filter((choice) => !writesAnswer(choice));
      const shared = { ...parts[0], choices: oneTap, recommended: agreed };
      // While one choice is being sent to every run, the whole card is inert:
      // a second choice, or a run's own answer, could otherwise answer some
      // runs one way and the rest another.
      let sending = false;
      const answerAll = async (picked) => {
        if (sending) throw new Error("This group is still sending an answer.");
        sending = true;
        article.inert = true;
        article.classList.add("sending");
        const failed = [];
        try {
          for (const [at, item] of items.entries()) {
            const choice = parts[at].choices.find((own) => own.key === picked.key);
            try {
              await ctx.api.answer(item.runId, answerBody(item, [], choice.line));
            } catch (error) {
              failed.push(`${short(item.runId)}: ${error.message}`);
            }
          }
        } finally {
          sending = false;
          article.inert = false;
          article.classList.remove("sending");
        }
        refresh();
        if (failed.length) throw new Error(`${failed.length} of ${items.length} answers failed:\n${failed.join("\n")}`);
      };
      const buttons = choiceButtons(shared, null, answerAll);
      const members = items.map((item, at) =>
        el(
          "li",
          {},
          el(
            "details",
            {},
            el("summary", {}, parts[at].headline, " ", el("span", { class: "muted small" }, "paused ", ago(item.pausedAt))),
            el("div", { class: "item-head" }, runLink(item)),
            panelOf(item),
          ),
        ),
      );
      const article = el(
        "article",
        { class: "panel attention-group" },
        el(
          "header",
          { class: "item-head" },
          el("strong", {}, `${items.length} runs ask the same question`),
          el("span", { class: "muted" }, items[0].project?.key ?? ""),
        ),
        el(
          "div",
          { class: "attention-box" },
          buttons.length ? el("p", { class: "muted small" }, `A choice below answers all ${items.length}.`) : null,
          buttons.length ? el("div", { class: "choices" }, buttons) : null,
          el("ul", { class: "attention-members" }, members),
        ),
      );
      return article;
    }

    load();
    return ctx.live.subscribe(load);
  },
};
