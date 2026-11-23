// Small DOM helpers shared by every console page.
import { formatAgo, formatDuration } from "../format.js";
import { runTone, secondsBetween, toIso } from "./model.js";

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (key === "value") node.value = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  append(node, children);
  return node;
}

export function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false || child === "") continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function replace(node, ...children) {
  node.replaceChildren();
  return append(node, children);
}

export function badge(run) {
  const tone = runTone(run);
  return el("span", { class: `pill tone-${tone}` }, tone);
}

export function ago(iso) {
  const normal = toIso(iso);
  return normal ? el("time", { datetime: normal, title: new Date(normal).toLocaleString() }, formatAgo(normal, Date.now())) : "";
}

export function runDuration(run) {
  const seconds = secondsBetween(run.started_at, run.completed_at, Date.now());
  return seconds === null ? "" : formatDuration(seconds);
}

export function link(path, ...children) {
  return el("a", { href: `#${path}` }, ...children);
}

export function notice(text, tone = "muted") {
  return el("p", { class: `notice ${tone}` }, text);
}

export function section(title, ...children) {
  return el("section", { class: "panel" }, el("h2", {}, title), ...children);
}

// A button that runs an async action once at a time and shows its failure.
export function actionButton(label, action, { tone = "", confirm: question = null } = {}) {
  const button = el("button", { type: "button", class: tone });
  button.textContent = label;
  button.addEventListener("click", async () => {
    if (question && !window.confirm(question)) return;
    button.disabled = true;
    try {
      await action();
    } catch (error) {
      window.alert(error.message);
    } finally {
      button.disabled = false;
    }
  });
  return button;
}

export function short(id) {
  return typeof id === "string" ? id.slice(0, 8) : "";
}
