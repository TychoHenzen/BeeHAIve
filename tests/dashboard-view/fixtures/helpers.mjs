export function findNode(node, predicate) {
  if (predicate(node)) return node;
  for (const child of node.children) {
    const result = findNode(child, predicate);
    if (result) return result;
  }
  return null;
}
