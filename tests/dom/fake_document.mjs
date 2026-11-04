import { FakeNode } from "./fake_node.mjs";

export class FakeDocument extends FakeNode {
  constructor(ids = []) {
    super("document");
    for (const id of ids) this.append(new FakeNode("div", id));
  }

  querySelector(selector) {
    if (selector.startsWith("#")) {
      return this.children.find((child) => child.id === selector.slice(1)) ?? null;
    }
    return super.querySelector(selector);
  }

  createElement(tagName) {
    return new FakeNode(tagName);
  }

  createElementNS(_namespace, tagName) {
    return new FakeNode(tagName);
  }

  createTextNode(value) {
    const node = new FakeNode("span");
    node.textContent = value;
    return node;
  }
}
