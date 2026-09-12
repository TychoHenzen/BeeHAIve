# Worker task contracts

Each implementation run stores one versioned contract before the model call.
The current inventory contract identifies the `inventory` step, its bounded
inputs, the `read_repository` capability, its required artifacts, and the
allowed terminal outcomes.

The worker must return one JSON object with this shape:

```json
{
  "outcome": "pass|fail|blocked|question",
  "evidence": {},
  "artifact_refs": [],
  "question": null,
  "required_action": null,
  "validation_reason": null
}
```

The worker always includes `evidence` and `artifact_refs`, even when empty.
Only the operator answer endpoint supplies an answer.

`pass` requires the contract's evidence and artifacts. `fail` follows the
existing bounded retry and escalation route. `blocked` records the required
operator action and stops automatic work. `question` records an operator
question, reopens bounded routing, and becomes claimable again after
`POST /runs/{run_id}/question/answer` records an answer.

Unknown versions, outcomes, fields, malformed JSON, undeclared artifacts, and
missing required artifacts fail closed. Evidence and operator text are bounded
and redacted before durable storage.
