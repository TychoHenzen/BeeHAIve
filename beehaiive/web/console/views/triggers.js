// Trigger bindings and their schedules: the part of Archon its web UI leaves out.
// A binding file says which workflow `archon trigger fire` starts. Archon only
// installs schedules on macOS, so here each binding can get a Windows Task
// Scheduler task named Archon-<binding> that fires it on its interval.
import { actionButton, ago, el, notice, replace, section } from "../dom.js";
import { BINDING_PRESETS, formatInterval, joinPath, presetBinding } from "../model.js";

const SELF_INPUT = { "build-queue": "build_queue_binding", "merge-queue": "merge_queue_binding" };
const PRESET_LABELS = {
  "pbi-refine-queue": "PBI refine queue (every 30 min)",
  "pbi-build-queue": "PBI build queue (every 30 min)",
  "pbi-merge-queue": "PBI merge queue (every 30 min)",
  "pbi-discovery-queue": "PBI discovery queue (every 4 h)",
  "pbi-upkeep-queue": "PBI upkeep queue (daily)",
  "build-queue": "SDLC pack build queue (every 30 min)",
  "merge-queue": "SDLC pack merge queue (every 30 min)",
  discoveries: "SDLC pack discoveries (every 4 h)",
  "worktree-cleanup": "Worktree cleanup (every 6 h)",
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

export default {
  id: "triggers",
  title: "Triggers",
  nav: true,
  match: (path) => (path === "/triggers" ? {} : null),
  mount(root, params, ctx) {
    const state = { directory: "", bindings: [], tasks: new Map(), scheduling: true, identity: null, projects: [], defaultHome: true };
    const bindingList = el("div", {});
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
      section("Bindings", bindingList),
      section("New binding", formSlot),
      section("Recent trigger receipts", receiptList),
    );

    function taskFor(name) {
      return state.tasks.get(`Archon-${name}`) ?? null;
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
        actionButton("Unschedule", async () => (await ctx.api.removeSchedule(binding.name), loadBindings()), {
          confirm: `Delete the Windows task Archon-${binding.name}? The binding file stays.`,
        }),
      );
    }

    function bindingCard(binding) {
      const output = el("div", {});
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
        binding.error
          ? notice(binding.error, "error")
          : el(
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
            ),
        el(
          "div",
          { class: "row" },
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
          actionButton("Delete", async () => (await ctx.api.deleteBinding(binding.name), loadBindings()), {
            confirm: `Delete the binding ${binding.name}? Its file goes, and so does its Windows task if it has one. Archon keeps the history of past fires.`,
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
      const fields = {
        name: el("input", { type: "text", required: true }),
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

      function apply() {
        const chosen = state.projects.find((p) => p.id === project.value);
        if (!preset.value || !chosen) return;
        const filled = presetBinding(preset.value, { project: chosen, directory: state.directory, hostId: fields.hostId.value || "local" });
        fields.name.value = filled.name;
        fields.workflow.value = filled.workflow;
        fields.cwd.value = filled.cwd;
        fields.resource.value = filled.resource;
        fields.minutes.value = String(filled.intervalSeconds / 60);
        fields.overlap.value = filled.overlap;
        fields.capacity.value = String(filled.capacity);
        fields.isolation.value = filled.isolation;
        fields.inputs.value = inputsText(filled.inputs);
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

      const label = (text, control, hint) => el("label", { class: "field" }, el("span", {}, text), control, hint ? el("span", { class: "muted small" }, hint) : null);
      const save = actionButton(
        "Save binding",
        async () => {
          if (!state.identity?.runAsUserId) throw new Error("archon trigger whoami did not give a user id.");
          await ctx.api.createBinding({
            name: fields.name.value,
            workflow: fields.workflow.value,
            cwd: fields.cwd.value,
            resource: fields.resource.value,
            hostId: fields.hostId.value,
            runAsUserId: state.identity.runAsUserId,
            intervalSeconds: Math.round(Number(fields.minutes.value) * 60),
            overlap: fields.overlap.value,
            capacity: Number(fields.capacity.value),
            isolation: fields.isolation.value,
            inputs: parseInputs(fields.inputs.value),
          });
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
          label("Workflow", fields.workflow),
          label("Runs in", fields.cwd, "The folder the workflow starts in."),
          label("Resource", fields.resource, "Runs that share a resource share its capacity."),
          label("Every (minutes)", fields.minutes),
          label("Overlap", fields.overlap, "skip drops a start while the resource is full; queue waits for a slot."),
          label("Capacity", fields.capacity),
          label("Isolation", fields.isolation, "worktree gives each run its own checkout; default uses the project's setting."),
          label("Host id", fields.hostId, "Names this machine to archon trigger drain."),
          label("Inputs", fields.inputs, "One name=value per line."),
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
