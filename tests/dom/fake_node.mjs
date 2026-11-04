import { camelCase, selectorMatches } from "./selectors.mjs";

function classListFor(node) {
  const names = () => node.className.split(/\s+/);
  return {
    add: (...added) => {
      const merged = `${node.className} ${added.join(" ")}`.trim().split(/\s+/);
      node.className = [...new Set(merged)].join(" ");
    },
    remove: (...removed) => {
      node.className = names().filter((name) => !removed.includes(name)).join(" ");
    },
    toggle: (name, force) => {
      const hasName = names().includes(name);
      const shouldHave = force ?? !hasName;
      if (shouldHave && !hasName) node.className = `${node.className} ${name}`.trim();
      if (!shouldHave && hasName) {
        node.className = names().filter((item) => item !== name).join(" ");
      }
    },
  };
}

export class FakeNode {
  constructor(tagName, id = "") {
    this.tagName = tagName.toUpperCase();
    this.id = id;
    this.children = [];
    this.listeners = new Map();
    this.attributes = {};
    this.dataset = {};
    this.style = {};
    this.className = "";
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this._text = "";
  }

  set textContent(value) {
    this._text = String(value);
    this.children = [];
  }

  get textContent() {
    return this._text + this.children.map((child) => child.textContent).join("");
  }

  get classList() {
    return classListFor(this);
  }

  append(...nodes) {
    this.children.push(...nodes);
  }

  replaceChildren(...nodes) {
    this.children = nodes;
    this._text = "";
  }

  addEventListener(type, listener) {
    this.listeners.set(type, listener);
  }

  dispatch(type, event = {}) {
    const dispatched = {
      ...event,
      target: this,
      currentTarget: this,
      defaultPrevented: false,
      preventDefault() {
        dispatched.defaultPrevented = true;
      },
    };
    const listener = this.listeners.get(type);
    if (listener) listener(dispatched);
    return dispatched;
  }

  click() {
    return this.dispatch("click");
  }

  setAttribute(attribute, value) {
    this.attributes[attribute] = String(value);
    if (attribute.startsWith("data-")) this.dataset[camelCase(attribute)] = String(value);
  }

  removeAttribute(attribute) {
    delete this.attributes[attribute];
    if (attribute.startsWith("data-")) delete this.dataset[camelCase(attribute)];
  }

  querySelectorAll(selector) {
    const matches = [];
    const visit = (node) => {
      for (const child of node.children) {
        if (selectorMatches(child, selector)) matches.push(child);
        visit(child);
      }
    };
    visit(this);
    return matches;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] ?? null;
  }

  getBoundingClientRect() {
    return { left: 0, top: 0, width: 780, height: 220 };
  }

  setPointerCapture() {}

  focus() {}
}
