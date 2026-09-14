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
existing bounded retry and escalation route. `blocked` and `question` create a
durable operator question and move the run to `awaiting_operator`. The run stays
nonclaimable until an authorized answer includes the current question ID and
revision at `POST /runs/{run_id}/question/answer`. The answer is recorded on the
same run, which can then resume under a new lease. Repeated identical answers
are safe. Stale question IDs or revisions and different answer replays are
rejected.

New questions and exhausted routing can notify one optional HTTPS webhook.
Configure `BEEHAIIVE_NOTIFICATION_WEBHOOK_URL` and
`BEEHAIIVE_NOTIFICATION_WEBHOOK_SECRET`. Delivery uses bearer authentication,
records its status, and makes at most three attempts for transient failures.
Without both settings, the question remains available in the dashboard and its
notification status is `not_configured`. Answering a question cancels its
undelivered notification. Stopping the run closes its pending question and
cancels any queued notification. Startup drains at most 25 queued notifications
in one batch; new questions are dispatched when created. SQLite notification
leases assume one API process per database.

Unknown versions, outcomes, fields, malformed JSON, undeclared artifacts, and
missing required artifacts fail closed. Evidence and operator text are bounded
and redacted before durable storage.
