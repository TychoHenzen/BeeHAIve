// The console shell: a hash router over a registry of pages.
//
// To add a page, write a module in views/ that exports
//   { id, title, nav, match(path) -> params | null, mount(root, params, ctx) -> cleanup }
// and list it in VIEWS. mount gets ctx = { api, live, navigate, refreshAttention }
// and returns a function that undoes whatever it started (timers, live listeners).
import { api, liveFeed } from "./api.js";
import { el, replace } from "./dom.js";
import attention from "./views/attention.js";
import launch from "./views/launch.js";
import projects from "./views/projects.js";
import run from "./views/run.js";
import runs from "./views/runs.js";
import triggers from "./views/triggers.js";

const VIEWS = [runs, attention, launch, projects, triggers, run];
const HOME = "/runs";

const root = document.getElementById("view");
const nav = document.getElementById("nav");
const liveState = document.getElementById("live-state");
const archonLink = document.getElementById("archon-link");

const LIVE_TEXT = { live: "Live", reconnecting: "Reconnecting...", offline: "Archon offline" };
const live = liveFeed((state) => {
  liveState.textContent = LIVE_TEXT[state] ?? state;
  liveState.dataset.state = state;
});

const navLinks = new Map();
const attentionCount = el("span", { class: "count", hidden: true });
for (const view of VIEWS.filter((v) => v.nav)) {
  const link = el("a", { href: `#/${view.id}` }, view.title, view.id === "attention" ? attentionCount : null);
  navLinks.set(view.id, link);
  nav.append(link);
}

async function refreshAttention() {
  try {
    const queue = await api.attention();
    const count = (queue.items ?? []).length;
    attentionCount.textContent = String(count);
    attentionCount.hidden = count === 0;
  } catch {
    attentionCount.hidden = true;
  }
}

function parseHash() {
  const raw = location.hash.replace(/^#/, "") || HOME;
  const at = raw.indexOf("?");
  const path = at === -1 ? raw : raw.slice(0, at);
  const query = Object.fromEntries(new URLSearchParams(at === -1 ? "" : raw.slice(at + 1)));
  return { path, query };
}

function navigate(path) {
  location.hash = `#${path}`;
}

const ctx = { api, live, navigate, refreshAttention };
let cleanup = () => {};

function route() {
  const { path, query } = parseHash();
  let view = null;
  let params = null;
  for (const candidate of VIEWS) {
    params = candidate.match(path);
    if (params) {
      view = candidate;
      break;
    }
  }
  if (!view) {
    navigate(HOME);
    return;
  }
  cleanup();
  for (const [id, link] of navLinks) {
    if (id === view.id || (view.id === "run" && id === "runs")) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  document.title = `${view.title} - BeeHAIve`;
  replace(root);
  root.scrollTop = 0;
  cleanup = view.mount(root, { ...query, ...params }, ctx) ?? (() => {});
}

window.addEventListener("hashchange", route);
live.subscribe(() => refreshAttention());
setInterval(refreshAttention, 30000);
api
  .settings()
  .then((settings) => {
    if (settings?.url) archonLink.href = `${settings.url}/console`;
  })
  .catch(() => {});
refreshAttention();
route();
