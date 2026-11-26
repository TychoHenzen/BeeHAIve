// Trigger bindings and their schedules: the part of Archon its web UI leaves out.
// A binding file says which workflow `archon trigger fire` starts. Archon only
// installs schedules on macOS, so here each binding can get a Windows Task
// Scheduler task named Archon-<binding> that fires it on its interval.
import { actionButton, ago, el, notice, replace, section } from "../dom.js";
import { BINDING_PRESETS, formatInterval, joinPath, pauseState, presetBinding } from "../model.js";
import { bindingFields, label, workflowCache } from "./binding-form.js";
import { restartPanel } from "./restart-panel.js";

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
    // saved: the binding an edit just saved, so its reloaded card can say so.
    // paused: every Archon task is disabled, so nothing may fire or be scheduled.
    const state = { directory: "", bindings: [], tasks: new Map(), scheduling: true, paused: false, identity: null, defaultHome: true, saved: null };
    // What the binding forms choose from.
    const page = { projects: [], bindings: [], workflowsFor: workflowCache(ctx.api) };
    const bindingList = el("div", {});
    const formSlot = el("div", {});
    const receiptList = el("div", {});
    const restart = restartPanel(ctx, {
      onChange: async () => {
        await loadBindings();
        ctx.refreshTriggers();
      },
    });
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
      section("Restart safely", restart.root),
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
        if (state.paused) return el("span", { class: "muted small" }, "Resume the triggers to schedule it.");
        return actionButton("Schedule it", async () => (await ctx.api.installSchedule(binding.name), loadBindings()), {
          confirm: `Create the Windows task Archon-${binding.name}? It fires this binding ${formatInterval(binding.intervalSeconds)} while you are signed in.`,
          tone: "primary",
        });
      }
      return el(
        "div",
        { class: "schedule" },
        el("span", { class: `pill tone-${task.state === "Ready" || task.state === "Running" ? "running" : "paused"}` }, task.state),
        task.nextRun && task.state !== "Disabled" ? el("span", { class: "small" }, " next ", new Date(task.nextRun).toLocaleTimeString()) : null,
        task.lastRun ? el("span", { class: "small muted" }, " last ", ago(task.lastRun), ` (result ${task.lastResult})`) : null,
        actionButton("Unschedule", async () => (await ctx.api.removeSchedule(binding.name), loadBindings()), {
          confirm: `Delete the Windows task Archon-${binding.name}? The binding file stays.`,
        }),
      );
    }

    // The card's facts, or in their place the same fields the New binding form
    // has, filled from the file. Saving keeps the name, ids and run-as user.
    function editForm(binding, close) {
      const settings = bindingFields(page, { self: binding.name, selfPath: () => binding.path });
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

    function fireButton(binding, output) {
      if (state.paused) {
        return el("button", { type: "button", disabled: true, title: "Triggers are paused for a restart. Resume them to fire." }, "Fire now");
      }
      return actionButton(
        "Fire now",
        async () => {
          replace(output, notice("Firing..."));
          const result = await ctx.api.fire(binding.name);
          replace(output, el("pre", { class: `inline ${result.ok ? "" : "error"}` }, result.output || (result.ok ? "Fired." : `Exit ${result.code}`)));
          loadReceipts();
        },
        { confirm: `Fire ${binding.name} now? It starts ${binding.workflow} the same way its schedule would.` },
      );
    }

    function bindingCard(binding) {
      const output = el("div", {});
      const body = el("div", {}, binding.error ? notice(binding.error, "error") : facts(binding));
      const edit = binding.error
        ? null
        : el(
            "button",
            {
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
            },
            "Edit",
          );
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
        body,
        el(
          "div",
          { class: "row" },
          edit,
          fireButton(binding, output),
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
        page.bindings = state.bindings;
        state.defaultHome = listing.defaultHome !== false;
        state.scheduling = schedules.supported !== false;
        const tasks = schedules.tasks ?? [];
        state.tasks = new Map(tasks.map((t) => [t.name, t]));
        state.paused = pauseState(tasks).state === "paused";
        restart.show(schedules.error ? null : tasks, schedules.error?.message);
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
      const preset = el(
        "select",
        { "aria-label": "Preset" },
        Object.keys(BINDING_PRESETS).map((k) => el("option", { value: k }, PRESET_LABELS[k])),
        el("option", { value: "" }, "Custom"),
      );
      const name = el("input", { type: "text", required: true });
      const selfPath = () => (name.value.trim() ? joinPath(state.directory, `${name.value.trim()}.json`) : "");
      const settings = bindingFields(page, { self: null, selfPath, onProject: (project) => apply(project) });

      // A preset fills every field for the chosen project; Custom leaves them.
      function apply(project) {
        if (!preset.value || !project) return false;
        const hostId = settings.fields.hostId.value.trim() || "local";
        const filled = presetBinding(preset.value, { project, directory: state.directory, hostId });
        name.value = filled.name;
        settings.fill(filled, { picked: false });
        return true;
      }

      name.addEventListener("input", () => settings.renamed());
      preset.addEventListener("change", () => apply(settings.project()));
      const first = page.projects.find((p) => p.id === params.project) ?? page.projects[0] ?? null;
      if (!apply(first)) settings.fill({ cwd: first?.default_cwd ?? "", hostId: "local" }, { picked: false });

      const save = actionButton(
        "Save binding",
        async () => {
          if (!state.identity?.runAsUserId) throw new Error("archon trigger whoami did not give a user id.");
          await ctx.api.createBinding({ ...settings.read(), name: name.value.trim(), runAsUserId: state.identity.runAsUserId });
          await loadBindings();
          renderForm();
        },
        { tone: "primary" },
      );
      replace(
        formSlot,
        el(
          "div",
          { class: "binding-form" },
          label("Preset", preset, "Fills the fields with that workflow's usual settings. Change anything before saving."),
          label("Name", name, "The file name and the binding id."),
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
      page.projects = Array.isArray(projects) ? projects : [];
      state.identity = who.identity;
      loadBindings().then(renderForm);
    });
    loadReceipts();
    return () => restart.stop();
  },
};
