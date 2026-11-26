// The controls for answering one paused run, shared by the run page and the
// attention queue. An "attention" pause is answered in its declared form; an
// "awaiting_response" pause is a gate resolved with one of its decisions.
import { actionButton, el, notice } from "./dom.js";
import { answerBody, answerReady, attentionText, expectedGate, gateDecisions, questionParts, writesAnswer } from "./model.js";

// The message as a headline anyone can read at a glance, with the rest folded
// under it. `parts` comes from questionParts.
function messageView(parts) {
  return [
    el("p", { class: "attention-headline" }, parts.headline || "Waiting for an answer."),
    parts.more
      ? el("details", { class: "attention-more" }, el("summary", {}, "Details"), el("div", { class: "attention-message" }, parts.more))
      : null,
  ];
}

// One button per lettered choice. A choice that needs words of the owner's
// own focuses `text` instead of sending. `send(choice)` answers with it.
export function choiceButtons(parts, text, send, label = (choice) => `(${choice.key}) ${choice.says}`) {
  return parts.choices.map((choice) => {
    const recommended = parts.recommended === choice.key;
    const tone = `choice${recommended ? " recommended" : ""}`;
    if (writesAnswer(choice)) {
      return el("button", { type: "button", class: tone, onclick: () => text?.focus() }, label(choice));
    }
    const button = actionButton(label(choice), () => send(choice), { tone });
    if (recommended) button.append(" ", el("span", { class: "pill tone-recommended" }, "recommended"));
    return button;
  });
}

// A message-form question: its choices as buttons, and a box for any other
// answer.
function messageAnswer(runId, attention, parts, ctx, onDone) {
  const text = el("textarea", { rows: "2", placeholder: parts.choices.length ? "Or write your own answer" : "Your answer" });
  const send = async (answer) => {
    await ctx.api.answer(runId, answerBody(attention, [], answer));
    onDone();
  };
  const submit = actionButton(
    "Send answer",
    async () => {
      if (!answerReady(attention, [], text.value)) throw new Error("Write an answer first.");
      await send(text.value);
    },
    { tone: parts.choices.length ? "" : "primary" },
  );
  return el(
    "div",
    { class: "answer" },
    parts.choices.length ? el("div", { class: "choices" }, choiceButtons(parts, text, (choice) => send(choice.line))) : null,
    parts.why ? el("p", { class: "muted small" }, parts.why) : null,
    text,
    el("div", { class: "row" }, submit),
  );
}

// A select or approval form with declared options: each option is one tap,
// sent with whatever the box holds as the reason.
function pickAnswer(runId, attention, ctx, onDone) {
  const text = el("textarea", { rows: "2", placeholder: "Why (optional, sent with your pick)" });
  const buttons = attention.options.map((option) => {
    const recommended = attention.recommendation?.option === option.id;
    const button = actionButton(
      `${option.label ?? option.id}${option.does ? ` - ${option.does}` : ""}`,
      async () => {
        await ctx.api.answer(runId, answerBody(attention, [option.id], text.value));
        onDone();
      },
      { tone: `choice${recommended ? " recommended" : ""}` },
    );
    if (recommended) button.append(" ", el("span", { class: "pill tone-recommended" }, "recommended"));
    return button;
  });
  const why = attention.recommendation?.why
    ? el("p", { class: "muted small" }, `Recommendation (${attention.recommendation.confidence ?? "no"} confidence): ${attention.recommendation.why}`)
    : null;
  return el("div", { class: "answer" }, el("div", { class: "choices" }, buttons), why, text);
}

// A multi_select form: tick any number, then send.
function multiAnswer(runId, attention, ctx, onDone) {
  const picked = new Set();
  const text = el("textarea", { rows: "2", placeholder: "Why (optional)" });
  const options = (attention.options ?? []).map((option) => {
    const input = el("input", {
      type: "checkbox",
      name: `answer-${runId}`,
      value: option.id,
      onchange: () => (input.checked ? picked.add(option.id) : picked.delete(option.id)),
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
  const submit = actionButton(
    "Send answer",
    async () => {
      const choice = [...picked];
      if (!answerReady(attention, choice, text.value)) throw new Error("Pick at least one option first.");
      await ctx.api.answer(runId, answerBody(attention, choice, text.value));
      onDone();
    },
    { tone: "primary" },
  );
  return el("div", { class: "answer" }, el("div", { class: "options" }, options), text, el("div", { class: "row" }, submit));
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
  const asks = attention.kind === "attention" || attention.kind === "awaiting_response";
  const parts = asks ? questionParts(attentionText(attention), { lettered: attention.form === "message" }) : null;
  const box = el("div", { class: "attention-box" }, parts ? messageView(parts) : el("p", { class: "attention-message" }, attentionText(attention)));
  if (attention.kind === "attention") {
    if (attention.form === "message") box.append(messageAnswer(runId, attention, parts, ctx, onDone));
    else if (attention.form === "multi_select") box.append(multiAnswer(runId, attention, ctx, onDone));
    else if (attention.options?.length) box.append(pickAnswer(runId, attention, ctx, onDone));
    else box.append(notice(`This ${attention.form ?? "question"} declares no options to pick from; answer it with archon workflow answer.`));
  } else if (attention.kind === "awaiting_response") {
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
