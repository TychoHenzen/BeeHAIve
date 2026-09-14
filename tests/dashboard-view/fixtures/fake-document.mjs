import { FakeNode } from "./fake-node.mjs";

export class FakeDocument {
  createElement(tag) {
    return new FakeNode(tag);
  }
}
