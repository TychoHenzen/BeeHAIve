export function settle() {
  return new Promise((resolve) => setImmediate(resolve));
}

export function response(payload) {
  return { ok: true, json: async () => payload };
}
