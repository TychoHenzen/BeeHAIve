# Completed-session meta-review

The meta-review reads only completed BeeHAIve runs persisted in the state
store. A source record is identified by `run:<run_id>` and includes the final
result or error, lifecycle events, and routing attempts. It does not read Codex
transcripts, credentials, private files, or active runs.

The review is explicit and bounded. The API accepts at most 25 records and
8,000 estimated input tokens. Results, errors, event details, analyzer text,
and evidence references are redacted and truncated before storage. A second
review cannot overlap a running review for the same state store.

Run a review with an API key:

```powershell
curl.exe -X POST http://127.0.0.1:8000/projects/<project-id>/meta-review `
  -H "X-API-Key: <api-key>" -H "Content-Type: application/json" `
  -d '{"record_limit":25,"input_token_limit":8000}'
```

Inspect suggestions without mutation:

```powershell
curl.exe http://127.0.0.1:8000/projects/<project-id>/meta-review/suggestions
```

Accept or reject one suggestion explicitly:

```powershell
curl.exe -X POST http://127.0.0.1:8000/projects/<project-id>/meta-review/suggestions/<suggestion-id> `
  -H "X-API-Key: <api-key>" -H "Content-Type: application/json" `
  -d '{"decision":"accept"}'
```

Suggestions use a stable identity per project and suggestion key. Repeating a
review updates the evidence and keeps the operator decision. The analyzer is
deterministic in this version. It proposes workflow outcomes from routing
failures, missing routing evidence, or a completed handoff.

The API never creates GitHub issues, changes Project items, or mutates provider
state. An operator must inspect and decide each suggestion through the API.
