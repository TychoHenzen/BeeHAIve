# Completed-session meta-review

The meta-review reads only completed BeeHAIve runs persisted in the state
store. A source record is identified by `run:<run_id>` and includes the final
result or error, lifecycle events, routing attempts, and bounded worker transcript
excerpts. Transcript sources include assistant messages and fixed progress names
only. Task prompts, tool arguments/results, raw stdout/stderr, private reasoning,
environment values, and tool-supplied file contents are excluded.

Session storage retains the latest 100 events, at most 4,000 characters each and
64,000 UTF-8 text bytes per run. Oldest events are evicted first. Known worker
secrets and credential patterns are redacted before truncation and persistence.
The existing session rows keep their current database lifetime. There is no
second archive, backfill, or time-based expiry. These are per-run limits.

Analysis selects the newest 20 allowed events in ascending persisted sequence,
with at most 500 characters per redacted excerpt. References use
`run:<run_id>:transcript:<sequence>` and remain stable across trimming and restart.
They identify evidence observed during review, not permanent full transcripts.
Pattern redaction and field limits are reapplied before analysis. All transcript
fields count toward the existing token estimate and whole-record admission limit.

Missing, malformed, oversized, trimmed, truncated, and interrupted evidence is
reported where observable. Every projection reports that capture is partial.
Legacy completed runs without sessions remain usable with a transcript gap.
Failed, cancelled, timed-out, and active runs remain excluded. Retained failed
attempts within a later completed run are diagnostic evidence, not proof of success.

The review is explicit and bounded. The API accepts at most 25 newest completed
records and 8,000 estimated input tokens; an optional `since` timestamp is
inclusive. Results, errors, event details, analyzer text, and evidence
references are redacted and truncated before storage. A second review cannot
overlap a running review for the same state store.

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

Suggestions use a stable identity per Project, normalized repository, and
versioned pattern family. A suggestion is emitted only when one structured
family is present in at least two distinct completed runs admitted to this
review. The supported families are routing failure, repair/retry, blocked
operator question, and retained `turn.failed` execution evidence. Duplicate
events, retries, transcript excerpts, repeated reviews, assistant prose, and
analyzer-provided counts do not add support. Repeating a qualifying review
updates the evidence and keeps the operator decision; a later non-qualifying
review leaves the historical suggestion untouched.

Review, listing, and rejection do not call the provider. An operator must
inspect and decide each suggestion through the API; explicit acceptance uses
the existing validated, idempotent PBI-creation handoff.
