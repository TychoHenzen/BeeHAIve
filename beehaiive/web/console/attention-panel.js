// The controls for answering one paused run, shared by the run page and the
// attention queue. An "attention" pause is answered in its declared form; an
// "awaiting_response" pause is a gate resolved with one of its decisions.
import { actionButton, el, notice } from "./dom.js";
import { answerBody, answerReady, attentionText, expectedGate, gateDecisions } from "./model.js";

function formAnswer(runId, attention, ctx, onDone) {
  const picked = new Set();
  const text = el("textarea", { rows: "3", placeholder: attention.form === "message" ? "Your answer" : "Why (optional)" });
  const multiple = attention.form === "multi_select";
  const options = (attention.options ?? []).map((option) => {
    const input = el("input", {
      type: multiple ? "checkbox" : "radio",
      name: `answer-${runId}`,
      value: option.id,
      onchange: () => {
        if (!multiple) picked.clear();
        if (input.checked) picked.add(option.id);
        else picked.delete(option.id);
      },
    });
    const recommended = attention.recommendation?.option === option.id;
    return el(
      "label",
      { class: `option${recommended ? " recommended" : ""}` },
      input,
      el("span", {}, el("strong", {}, option.label ?? option.id), option.does ? el("span", { class: "muted" }, ` - ${option.does}`) : null),
      recommended ? el("span", { class: "pill tone-recommended" }, "recommended") : null,
    );
  });
  const why = attention.recommendation?.why
    ? el("p", { class: "muted small" }, `Recommendation (${attention.recommendation.confidence ?? "no"} confidence): ${attention.recommendation.why}`)
    : null;
  const submit = actionButton(
    "Send answer",
    async () => {
      const choice = [...picked];
      if (!answerReady(attention, choice, text.value)) throw new Error("Pick an option (or write an answer) first.");
      await ctx.api.answer(runId, answerBody(attention, choice, text.value));
      onDone();
    },
    { tone: "primary" },
  );
  return el("div", { class: "answer" }, why, options.length ? el("div", { class: "options" }, options) : null, text, el("div", { class: "row" }, submit));
}

function gateAnswer(runId, approval, ctx, onDone) {
  const text = el("textarea", { rows: "3", placeholder: "Comment (optional, sent with the decision)" });
  const buttons = gateDecisions(approval).map((decision) =>
    actionButton(
      decision.label,
      async () => {
        const body = { decision: decision.id };
        if (text.value.trim()) body.text = text.value;
        const gate = expectedGate(approval);
        if (gate) body.expectedGate = gate;
        await ctx.api.respond(runId, body);
        onDone();
      },
      { tone: decision.id === "approve" ? "primary" : "" },
    ),
  );
  return el("div", { class: "answer" }, text, el("div", { class: "row" }, buttons));
}

// `attention` is the run's attention record or a queue item; `approval` is the
// run's metadata.approval when the caller has it.
export function attentionPanel(runId, attention, approval, ctx, onDone) {
  const box = el("div", { class: "attention-box" }, el("p", { class: "attention-message" }, attentionText(attention)));
  if (attention.kind === "attention") box.append(formAnswer(runId, attention, ctx, onDone));
  else if (attention.kind === "awaiting_response") {
    if (approval) box.append(gateAnswer(runId, approval, ctx, onDone));
    else {
      const slot = el("div", {}, notice("Loading the gate's decisions..."));
      box.append(slot);
      ctx.api
        .run(runId)
        .then((detail) => slot.replaceWith(gateAnswer(runId, detail.run?.metadata?.approval ?? {}, ctx, onDone)))
        .catch((error) => slot.replaceWith(notice(error.message, "error")));
    }
  } else if (attention.kind === "stalled_tool_calls") {
    box.append(
      el(
        "ul",
        { class: "small" },
        (attention.calls ?? []).map((call) => el("li", {}, `${call.name} in ${call.nodeId}, quiet for ${Math.round((call.stalledForMs ?? 0) / 1000)}s`)),
      ),
    );
  }
  return box;
}
