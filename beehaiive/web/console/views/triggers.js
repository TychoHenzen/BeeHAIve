// Trigger bindings and their schedules: the part of Archon its web UI leaves out.
// A binding file says which workflow `archon trigger fire` starts. Archon only
// installs schedules on macOS, so here each binding can get a Windows Task
// Scheduler task named Archon-<binding> that fires it on its interval.
import { actionButton, ago, el, link, notice, replace, section, short } from "../dom.js";
import { BINDING_PRESETS, formatInterval, joinPath, liveRunNote, presetBinding, stopLines } from "../model.js";

const SELF_INPUT = { "build-queue": "build_queue_binding", "merge-queue": "merge_queue_binding" };
const PRESET_LABELS = {
  "pbi-refine-queue": "PBI refine queue (every 30 min)",
  "pbi-build-queue": "PBI build queue (every 30 min)",
  "pbi-merge-queue": "PBI merge queue (every 30 min)",
  "pbi-discovery-queue": "PBI discovery queue (every 4 h)",
  "pbi-upkeep-queue": "PBI upkeep queue (daily)",
  "worktree-cleanup": "Worktree cleanup (every 6 h)",
  "daily-review": "Daily review of Archon runs (daily)",
  "build-queue": "SDLC pack build queue (every 30 min)",
  "merge-queue": "SDLC pack merge queue (every 30 min)",
  discoveries: "SDLC pack discoveries (every 4 h)",
};

function parseInputs(text) {
  const inputs = {};
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const at = trimmed.indexOf("=");
    if (at <= 0) throw new Error(`"${trimmed}" is not name=value.`);
    inputs[trimmed.slice(0, at).trim()] = trimmed.slice(at + 1).trim();
  }
  return inputs;
}

function inputsText(inputs) {
  return Object.entries(inputs ?? {})
    .map(([k, v]) => `${k}=${v}`)
    .join("\n");
}

const label = (text, control, hint) =>
  el("label", { class: "field" }, el("span", {}, text), control, hint ? el("span", { class: "muted small" }, hint) : null);

// The controls a new binding and an edit share. fill() sets them from a
// binding's values; read() gives the request body without the name and user.
function settingFields() {
  const fields = {
    workflow: el("input", { type: "text", required: true }),
    cwd: el("input", { type: "text", required: true }),
    resource: el("input", { type: "text", required: true }),
    hostId: el("input", { type: "text", value: "local" }),
    minutes: el("input", { type: "number", min: "1", value: "30" }),
    overlap: el("select", {}, el("option", { value: "skip" }, "skip"), el("option", { value: "queue" }, "queue")),
    capacity: el("input", { type: "number", min: "1", max: "32", value: "1" }),
    isolation: el("select", {}, ["worktree", "default", "in-place"].map((kind) => el("option", { value: kind }, kind))),
    inputs: el("textarea", { rows: "5", spellcheck: "false" }),
  };
  return {
    fields,
    fill(values) {
      fields.workflow.value = values.workflow ?? "";
      fields.cwd.value = values.cwd ?? "";
      fields.resource.value = values.resource ?? "";
      fields.hostId.value = values.hostId ?? "local";
      fields.minutes.value = Number.isFinite(values.intervalSeconds) ? String(values.intervalSeconds / 60) : "30";
      fields.overlap.value = values.overlap ?? "skip";
      fields.capacity.value = String(values.capacity ?? 1);
      fields.isolation.value = values.isolation ?? "worktree";
      fields.inputs.value = inputsText(values.inputs);
    },
    read: () => ({
      workflow: fields.workflow.value,
      cwd: fields.cwd.value,
      resource: fields.resource.value,
      hostId: fields.hostId.value,
      intervalSeconds: Math.round(Number(fields.minutes.value) * 60),
      overlap: fields.overlap.value,
      capacity: Number(fields.capacity.value),
      isolation: fields.isolation.value,
      inputs: parseInputs(fields.inputs.value),
    }),
    labels: () => [
      label("Workflow", fields.workflow),
      label("Runs in", fields.cwd, "The folder the workflow starts in."),
      label("Resource", fields.resource, "Runs that share a resource share its capacity."),
      label("Every (minutes)", fields.minutes),
      label("Overlap", fields.overlap, "skip drops a start while the resource is full; queue waits for a slot."),
      label("Capacity", fields.capacity, "How many runs on this resource may run at once."),
      label("Isolation", fields.isolation, "worktree gives each run its own checkout; default uses the project's setting."),
      label("Host id", fields.hostId, "Names this machine to archon trigger drain."),
      label("Inputs", fields.inputs, "One name=value per line."),
    ],
  };
}

export default {
  id: "triggers",
  title: "Triggers",
  nav: true,
  match: (path) => (path === "/triggers" ? {} : null),
  mount(root, params, ctx) {
    // saved: the binding an edit just saved, so its reloaded card can say so.
    // stops: by binding name, what its last unschedule or failed delete did,
    // kept until hidden so its live runs stay at hand.
    const state = { directory: "", bindings: [], tasks: new Map(), scheduling: true, identity: null, projects: [], defaultHome: true, saved: null, stops: new Map() };
    const bindingList = el("div", {});
    // What the last delete did, since its card is gone.
    const deletedSlot = el("div", {});
    const formSlot = el("div", {});
    const receiptList = el("div", {});
    replace(
      root,
      el(
        "div",
        { class: "page-head" },
        el("h1", {}, "Triggers and schedules"),
        el(
          "p",
          { class: "muted" },
          "A binding is a JSON file that tells archon trigger fire which workflow to start, where and as whom. ",
          "On Windows a scheduled task named Archon-<binding> fires it on its interval.",
        ),
      ),
      section("Bindings", deletedSlot, bindingList),
      section("New binding", formSlot),
      section("Recent trigger receipts", receiptList),
    );

    function taskFor(name) {
      return state.tasks.get(`Archon-${name}`) ?? null;
    }

    // A run the stopped binding started. Cancelling marks the run itself, so
    // the kept report shows it cancelled when the bindings redraw.
    function liveRun(run) {
      const row = el("li", {});
      const draw = () =>
        replace(
          row,
          link(`/runs/${run.id}`, el("code", {}, short(run.id))),
          run.workflow ? el("span", {}, run.workflow) : null,
          run.cancelled ? el("span", { class: "muted small" }, "Cancelled.") : el("span", { class: "small" }, liveRunNote(run)),
          run.startedAt ? el("span", { class: "muted small" }, "started ", ago(run.startedAt)) : null,
          run.cancelled
            ? null
            : actionButton(
                "Cancel run",
                async () => {
                  await ctx.api.cancel(run.id);
                  run.cancelled = true;
                  draw();
                },
                { confirm: `Cancel ${run.workflow ?? "this"} run ${short(run.id)}? It stops where it is.`, tone: "danger" },
              ),
        );
      draw();
      return row;
    }

    // report: { heading, tone, stop, retry }. Lists what was withdrawn, what
    // could not be, and the runs the binding started that are still live.
    function stopReport(report, hide) {
      const { stop } = report;
      const live = Array.isArray(stop?.live) ? stop.live : [];
      return el(
        "div",
        { class: "stop-report" },
        notice(report.heading, report.tone),
        stopLines(stop).map((line) => notice(line.text, line.tone)),
        live.length
          ? [
              el("p", { class: "notice" }, "Still live. Archon keeps these going until they end or you cancel them:"),
              el("ul", { class: "live-runs" }, live.map(liveRun)),
            ]
          : null,
        el(
          "div",
          { class: "row" },
          report.retry && !stop?.stopped ? actionButton("Try again", report.retry, { tone: "primary" }) : null,
          el("button", { type: "button", onclick: hide }, "Hide"),
        ),
      );
    }

    function cardReport(binding) {
      const report = state.stops.get(binding.name);
      if (!report) return null;
      const node = stopReport(report, () => {
        state.stops.delete(binding.name);
        node.remove();
      });
      return node;
    }

    // Removing the task stops new fires; the server then withdraws the
    // starts Archon still holds for the binding. Running it again is safe.
    async function unschedule(name) {
      const { removed, stop } = await ctx.api.removeSchedule(name);
      const heading = removed ? `Its Windows task Archon-${name} is gone.` : `It has no Windows task any more.`;
      state.stops.set(name, { heading, tone: "ok", stop, retry: () => unschedule(name) });
      await loadBindings();
    }

    // The server keeps the file when a queued start could not be withdrawn,
    // so the binding's card stays to say why and Delete can be tried again.
    async function deleteBinding(name) {
      try {
        const { stop } = await ctx.api.deleteBinding(name);
        state.stops.delete(name);
        const hide = () => replace(deletedSlot);
        replace(deletedSlot, stopReport({ heading: `Deleted ${name}.`, tone: "ok", stop, retry: null }, hide));
      } catch (error) {
        if (!error.data?.stop) throw error;
        const heading = "Not deleted. The binding file stays, so Delete can try again.";
        state.stops.set(name, { heading, tone: "error", stop: error.data.stop, retry: null });
      }
      await loadBindings();
    }

    function scheduleCell(binding) {
      if (!state.scheduling) return el("span", { class: "muted small" }, "Scheduling needs Windows.");
      const task = taskFor(binding.name);
      if (!task) {
        return actionButton("Schedule it", async () => (await ctx.api.installSchedule(binding.name), loadBindings()), {
          confirm: `Create the Windows task Archon-${binding.name}? It fires this binding ${formatInterval(binding.intervalSeconds)} while you are signed in.`,
          tone: "primary",
        });
      }
      return el(
        "div",
        { class: "schedule" },
        el("span", { class: `pill tone-${task.state === "Ready" || task.state === "Running" ? "running" : "paused"}` }, task.state),
        task.nextRun ? el("span", { class: "small" }, " next ", new Date(task.nextRun).toLocaleTimeString()) : null,
        task.lastRun ? el("span", { class: "small muted" }, " last ", ago(task.lastRun), ` (result ${task.lastResult})`) : null,
        actionButton("Unschedule", () => unschedule(binding.name), {
          confirm: `Delete the Windows task Archon-${binding.name} and withdraw the starts Archon still has queued for it? The binding file stays. Runs it already started keep going, and the page lists them so you can cancel them.`,
        }),
      );
    }

    // The card's facts, or in their place the same fields the New binding form
    // has, filled from the file. Saving keeps the name, ids and run-as user.
    function editForm(binding, close) {
      const settings = settingFields();
      settings.fill(binding);
      const save = actionButton(
        "Save changes",
        async () => {
          const { rescheduled } = await ctx.api.updateBinding(binding.name, settings.read());
          state.saved = { name: binding.name, rescheduled };
          await loadBindings();
        },
        { tone: "primary" },
      );
      return el(
        "div",
        { class: "binding-form" },
        settings.labels(),
        el(
          "p",
          { class: "muted small" },
          "The name, binding id and run-as user stay. The next fire reads the saved file. A new interval or folder also reinstalls its Windows task.",
        ),
        el("div", { class: "row" }, save, el("button", { type: "button", onclick: close }, "Cancel")),
      );
    }

    function facts(binding) {
      return el(
        "dl",
        { class: "facts" },
        el("dt", {}, "Runs in"),
        el("dd", {}, el("code", {}, binding.cwd ?? "")),
        el("dt", {}, "Resource"),
        el("dd", {}, `${binding.resource ?? ""} (capacity ${binding.capacity ?? "?"}, overlap ${binding.overlap ?? "?"})`),
        el("dt", {}, "Inputs"),
        el("dd", {}, el("pre", { class: "inline" }, inputsText(binding.inputs) || "none")),
        el("dt", {}, "File"),
        el("dd", {}, el("code", {}, binding.path)),
      );
    }

    function savedNotice(binding) {
      if (state.saved?.name !== binding.name) return null;
      const { rescheduled } = state.saved;
      state.saved = null;
      return notice(rescheduled ? `Saved. Its Windows task now fires ${formatInterval(binding.intervalSeconds)}.` : "Saved.");
    }

    function bindingCard(binding) {
      const output = el("div", {});
      const body = el("div", {}, binding.error ? notice(binding.error, "error") : facts(binding));
      const edit = binding.error
        ? null
        : el("button", {
            type: "button",
            onclick: () => {
              edit.hidden = true;
              replace(
                body,
                editForm(binding, () => {
                  edit.hidden = false;
                  replace(body, facts(binding));
                }),
              );
            },
          }, "Edit");
      return el(
        "article",
        { class: "panel binding" },
        el(
          "header",
          { class: "item-head" },
          el("strong", {}, binding.name),
          binding.workflow ? el("code", {}, binding.workflow) : null,
          el("span", { class: "muted small" }, formatInterval(binding.intervalSeconds)),
        ),
        savedNotice(binding),
        cardReport(binding),
        body,
        el(
          "div",
          { class: "row" },
          edit,
          actionButton(
            "Fire now",
            async () => {
              replace(output, notice("Firing..."));
              const result = await ctx.api.fire(binding.name);
              replace(output, el("pre", { class: `inline ${result.ok ? "" : "error"}` }, result.output || (result.ok ? "Fired." : `Exit ${result.code}`)));
              loadReceipts();
            },
            { confirm: `Fire ${binding.name} now? It starts ${binding.workflow} the same way its schedule would.` },
          ),
          scheduleCell(binding),
          actionButton("Delete", () => deleteBinding(binding.name), {
            confirm: `Delete the binding ${binding.name}? Its Windows task goes, the starts Archon still has queued for it are withdrawn, and then its file goes. Runs it already started keep going. Archon keeps the history of past fires.`,
            tone: "danger",
          }),
        ),
        output,
      );
    }

    async function loadBindings() {
      try {
        const [listing, schedules] = await Promise.all([ctx.api.bindings(), ctx.api.schedules().catch((error) => ({ supported: true, error }))]);
        state.directory = listing.directory;
        state.bindings = listing.bindings ?? [];
        state.defaultHome = listing.defaultHome !== false;
        state.scheduling = schedules.supported !== false;
        state.tasks = new Map((schedules.tasks ?? []).map((t) => [t.name, t]));
        replace(
          bindingList,
          schedules.error ? notice(`Task Scheduler: ${schedules.error.message}`, "error") : null,
          state.defaultHome ? null : notice("ARCHON_HOME is not the default. Scheduled tasks run archon with the default home.", "error"),
          state.bindings.length ? state.bindings.map(bindingCard) : notice(`No bindings yet. They live in ${state.directory}.`),
        );
      } catch (error) {
        replace(bindingList, notice(error.message, "error"));
      }
    }

    // Archon 0.14's `trigger list` rows: { id, sourceInstanceId, deliveryId,
    // outcome, reason, receivedAt }. A row without an id still shows as raw JSON.
    function receiptRow(receipt) {
      const id = typeof receipt?.id === "string" ? receipt.id : null;
      const detail = el("div", {});
      return el(
        "li",
        {},
        el(
          "div",
          { class: "item-head" },
          el("code", {}, id ? id.slice(0, 8) : "receipt"),
          receipt?.outcome ? el("span", { class: "pill" }, receipt.outcome) : null,
          receipt?.sourceInstanceId ? el("span", {}, receipt.sourceInstanceId) : null,
          receipt?.receivedAt ? el("span", { class: "muted small" }, ago(receipt.receivedAt)) : null,
          id
            ? actionButton("Inspect", async () => {
                const shown = await ctx.api.receipt(id);
                replace(detail, el("pre", { class: "json" }, shown.text ?? JSON.stringify(shown.receipt, null, 2)));
              })
            : null,
        ),
        receipt?.reason ? el("p", { class: "muted small" }, receipt.reason) : null,
        id ? null : el("pre", { class: "inline" }, JSON.stringify(receipt)),
        detail,
      );
    }

    async function loadReceipts() {
      try {
        const { receipts } = await ctx.api.receipts(25);
        const rows = Array.isArray(receipts) ? receipts : [];
        replace(receiptList, rows.length ? el("ul", { class: "receipts" }, rows.map(receiptRow)) : notice("No trigger has fired yet."));
      } catch (error) {
        replace(receiptList, notice(error.message, "error"));
      }
    }

    function renderForm() {
      const project = el("select", { "aria-label": "Project" }, state.projects.map((p) => el("option", { value: p.id, selected: p.id === params.project }, p.name)));
      const preset = el(
        "select",
        { "aria-label": "Preset" },
        Object.keys(BINDING_PRESETS).map((k) => el("option", { value: k }, PRESET_LABELS[k])),
        el("option", { value: "" }, "Custom"),
      );
      const settings = settingFields();
      const fields = { ...settings.fields, name: el("input", { type: "text", required: true }) };

      function apply() {
        const chosen = state.projects.find((p) => p.id === project.value);
        if (!preset.value || !chosen) return;
        const filled = presetBinding(preset.value, { project: chosen, directory: state.directory, hostId: fields.hostId.value || "local" });
        fields.name.value = filled.name;
        settings.fill(filled);
      }

      // A queue binding names its own file in its inputs; keep that in step with the name.
      fields.name.addEventListener("input", () => {
        const key = SELF_INPUT[preset.value];
        if (!key) return;
        const lines = fields.inputs.value.split("\n").map((line) =>
          line.startsWith(`${key}=`) ? `${key}=${joinPath(state.directory, `${fields.name.value}.json`)}` : line,
        );
        fields.inputs.value = lines.join("\n");
      });
      project.addEventListener("change", apply);
      preset.addEventListener("change", apply);
      apply();

      const save = actionButton(
        "Save binding",
        async () => {
          if (!state.identity?.runAsUserId) throw new Error("archon trigger whoami did not give a user id.");
          await ctx.api.createBinding({ ...settings.read(), name: fields.name.value, runAsUserId: state.identity.runAsUserId });
          await loadBindings();
        },
        { tone: "primary" },
      );
      replace(
        formSlot,
        el(
          "div",
          { class: "binding-form" },
          label("Project", project),
          label("Preset", preset, "Fills the fields with that workflow's usual settings. Change anything before saving."),
          label("Name", fields.name, "The file name and the binding id."),
          settings.labels(),
          el("p", { class: "muted small" }, "Runs as ", el("code", {}, state.identity?.runAsUserId ?? "unknown"), state.identity?.cliIdentity ? ` (${state.identity.cliIdentity})` : ""),
          el(
            "p",
            { class: "muted small" },
            "The PBI queues need gh signed in with push access, and quality-guard installed for build and merge. The SDLC pack's queues need its forge check to pass, a maintainers.json on the default branch and CI. Run each queue once by hand before you schedule it.",
          ),
          el("div", { class: "row" }, save),
        ),
      );
    }

    Promise.all([ctx.api.projects().catch(() => []), ctx.api.identity().catch(() => ({ identity: null }))]).then(([projects, who]) => {
      state.projects = Array.isArray(projects) ? projects : [];
      state.identity = who.identity;
      loadBindings().then(renderForm);
    });
    loadReceipts();
    return () => {};
  },
};
