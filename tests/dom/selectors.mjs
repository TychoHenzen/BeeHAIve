export function camelCase(attribute) {
  return attribute
    .replace(/^data-/, "")
    .replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
}

const ATTRIBUTE_SELECTOR = /^\[([^=\]]+)(?:=["']([^"']*)["'])?\]$/;

function attributeValue(node, name) {
  return name.startsWith("data-") ? node.dataset[camelCase(name)] : node.attributes[name];
}

export function selectorMatches(node, selector) {
  if (selector.startsWith("#")) return node.id === selector.slice(1);
  if (selector.startsWith(".")) {
    return String(node.className).split(/\s+/).includes(selector.slice(1));
  }
  const attribute = selector.match(ATTRIBUTE_SELECTOR);
  if (attribute) {
    const [, name, expected] = attribute;
    const value = attributeValue(node, name);
    return expected === undefined ? value !== undefined : String(value) === expected;
  }
  return node.tagName.toLowerCase() === selector.toLowerCase();
}
