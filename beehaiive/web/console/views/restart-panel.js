// Restart safely: pause every trigger task, wait until no run is going,
// restart (the machine, Archon or BeeHAIve), then resume. A fire runs its
// workflow in its own `archon trigger execute` process, so a restart cuts off
// whatever is running. A run cut off that way still holds its binding's
// resource, and every later fire of that binding is skipped until it ends.
import { actionButton, ago, el, link, notice, replace, short } from "../dom.js";
import { pauseState, restartRuns, restartVerdict } from "../model.js";

// Archon's process check reads the real processes, so it runs on a timer as
// well as on the live feed, which only says when a run's status changes.
const POLL_MS = 20000;
const ORDER = ["running", "starting", "interrupted", "waiting"];
const STATE_TEXT = { running: "running", starting: "starting", waiting: "waiting for an answer", interrupted: "interrupted" };
const STATE_TONE = { running: "running", starting: "running", waiting: "paused", interrupted: "failed" };
const plural = (count, one, many) => `${count} ${count === 1 ? one : many}`;

// onChange runs after a pause or resume, so the page reloads its tasks and
// hands them back through show(tasks), or show(null, message) when Task
// Scheduler could not list them.
export function restartPanel(ctx, { onChange }) {
  const root = el("div", {});
  // tasksError: Task Scheduler did not list its tasks, so whether they are
  // paused is unknown, which is not the same as there being none.
  const state = { tasks: null, tasksError: null, runs: [], processes: null, error: null };

  async function loadRuns() {
    try {
      const [running, pending, paused, processes] = await Promise.all([
        ctx.api.runs({ status: "running", limit: 50 }),
        ctx.api.runs({ status: "pending", limit: 50 }),
        ctx.api.runs({ status: "paused", limit: 50 }),
        ctx.api.archonProcesses().catch(() => null),
      ]);
      state.runs = [running, pending, paused].flatMap((listing) => listing?.runs ?? []);
      state.processes = processes;
      state.error = null;
    } catch (error) {
      state.error = error.message;
    }
    render();
  }

  function controls(pause) {
    const pauseButton = (text) =>
      actionButton(text, async () => (await ctx.api.pauseTriggers(), onChange()), {
        confirm: `Disable the ${plural(pause.total, "Archon task", "Archon tasks")}? Runs already going keep going. Nothing fires on its own until you resume, also after a restart.`,
        tone: "primary",
      });
    const resumeButton = (text) =>
      actionButton(text, async () => (await ctx.api.resumeTriggers(), onChange()), {
        confirm: "Enable every disabled Archon task? Each fires again on its own schedule, and a task that missed its time while paused fires once soon.",
      });
    switch (pause.state) {
      case "none":
        return [notice("No Windows task fires a binding, so nothing starts on its own.")];
      case "active":
        return [
          el("p", {}, "Pause the triggers first. Runs already going keep going, nothing new starts, and this panel says when the last run is done."),
          el("div", { class: "row" }, pauseButton("Pause triggers")),
        ];
      case "partial":
        return [
          el("p", { class: "paused-note" }, `${pause.disabled} of ${pause.total} trigger tasks are paused. The rest still fire.`),
          el("div", { class: "row" }, pauseButton("Pause the rest"), resumeButton("Resume all")),
        ];
      default:
        return [
          el("p", { class: "paused-note" }, `Triggers are paused. None of the ${plural(pause.total, "task", "tasks")} fires until you resume them, also after a restart.`),
          el("div", { class: "row" }, resumeButton("Resume triggers")),
        ];
    }
  }

  function verdictLine(pause, verdict, busy) {
    if (verdict === "safe") return notice("Nothing is running and nothing will start. It is safe to restart. Resume the triggers once it is back up.", "ok");
    if (verdict === "unpaused") return notice("Nothing is running right now, but a task can start a run at any moment.");
    const going = `${plural(busy, "run is", "runs are")} still going, and a restart now would cut ${busy === 1 ? "it" : "them"} off mid-step.`;
    return notice(pause.state === "paused" || pause.state === "none" ? `${going} This updates as they finish.` : `${going} Pause first so no new run starts.`);
  }

  function runRow({ run, binding, state: runState }) {
    const started = ago(run.started_at);
    return el(
      "li",
      {},
      el(
        "div",
        { class: "item-head" },
        el("span", { class: `pill tone-${STATE_TONE[runState]}` }, STATE_TEXT[runState]),
        link(`/runs/${run.id}`, el("code", {}, short(run.id))),
        el("strong", {}, run.workflow_name ?? "run"),
        binding ? el("span", { class: "muted small" }, `from ${binding}`) : el("span", { class: "muted small" }, "started by hand"),
        started ? el("span", { class: "muted small" }, "started ", started) : null,
        runState === "interrupted"
          ? actionButton("Cancel run", async () => (await ctx.api.cancel(run.id), loadRuns()), {
              confirm: `Cancel run ${short(run.id)}? Archon marks it cancelled${binding ? `, so the next fire of ${binding} can start` : ""}.`,
            })
          : null,
      ),
      runState === "interrupted"
        ? el(
            "p",
            { class: "muted small" },
            run.status === "pending" ? "No archon process ever picked it up. " : "The archon process that ran it is gone. ",
            binding ? `It still holds the resource of ${binding}, so every fire of ${binding} is skipped until it ends.` : "",
          )
        : null,
    );
  }

  function render() {
    if (state.tasksError) return replace(root, notice(`Task Scheduler did not list the Archon tasks, so this cannot pause them or say whether they are paused: ${state.tasksError}`, "error"));
    if (!state.tasks) return replace(root, notice("Loading..."));
    const pause = pauseState(state.tasks);
    const entries = restartRuns(state.runs, state.processes, Date.now()).sort((a, b) => ORDER.indexOf(a.state) - ORDER.indexOf(b.state));
    const { verdict, busy } = restartVerdict(pause, entries);
    replace(
      root,
      controls(pause),
      state.error ? notice(`Archon did not list its runs: ${state.error}`, "error") : verdictLine(pause, verdict, busy),
      state.processes ? null : notice("BeeHAIve could not list archon's processes, so a run whose process is gone still shows as running."),
      entries.length ? el("ul", { class: "restart-runs" }, entries.map(runRow)) : null,
    );
  }

  const unsubscribe = ctx.live.subscribe(loadRuns);
  const timer = setInterval(loadRuns, POLL_MS);
  loadRuns();
  return {
    root,
    show(tasks, tasksError = null) {
      state.tasks = tasks;
      state.tasksError = tasksError;
      render();
    },
    stop() {
      unsubscribe();
      clearInterval(timer);
    },
  };
}
