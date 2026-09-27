# Behavior designer

`/behavior-design?project=<owner>:<number>` is a separate, operator-gated surface for designing bounded automated-unit behaviors. It does not change the existing mission-control dashboard.

The flow is deliberately explicit:

1. Generate a JSON graph from the configured local Ollama-compatible model.
2. Inspect the states, transitions, allowlisted actions, and limits.
3. Save the draft and bind typed storage, factory, item, signal, color, and bounded wait references from the configured target catalog.
4. Confirm, assign a unit, and run the bounded example.

The API never executes model output directly. Invalid JSON, unknown actions or targets, missing bindings, overlarge graphs, ambiguous transitions, and unconfirmed behaviors fail closed. Behavior definitions, assignments, and per-action execution checkpoints are persisted in the state database so a later process can resume a running behavior with an idempotency key instead of replaying an external action. Use the designer's Reload readback button, or pass `behavior=<id>` in the URL, to inspect persisted assignment and execution state.

Production uses the injected `TargetUnitWorld`; an empty catalog fails safely until real project targets are configured. `RecordingUnitWorld` is only a test double.

The model endpoint defaults to `http://127.0.0.1:11434/api/chat`; configure `BEEHAIIVE_OLLAMA_URL`, `BEEHAIIVE_OLLAMA_MODEL`, and the existing API/workflow authorization settings as needed.
