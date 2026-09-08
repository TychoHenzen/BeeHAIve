we're using large volumes of very cheap models - what are the best "agent swarm" type software to direct continuous work, similar to uncle bob's swarm forge

Thought for 37s
That's a really clean design. And the good news is you probably don't need a framework at all — git is already your task queue, state machine, and handoff protocol. What you're describing is a small orchestrator daemon that watches git state and dispatches Codex tasks.

Here's how I'd think about it:

Git as the state machine. Issues carry labels that represent pipeline stages: stage/refine, stage/implement, stage/review, stage/triage, stage/human. A task moves forward when an agent relabels it. PRs are the handoff artifact between implement → review. The orchestrator's only job is: poll for work, match it to the next free thread, launch the right Codex task with the right prompt, and watch for completion.

The scheduler is simple because you have 4 threads and multiple stages. Round-robin or priority-weighted (e.g., reviews before new implementations, triage before refinement). A stuck thread that hasn't committed in N minutes gets killed and the task gets relabeled stage/triage.

Each stage is a prompt template + skill set, not a different agent binary. The refinement stage injects your /debate skill and the ticket text. The implementation stage injects language-specific coding conventions. The review stage fans out — you launch 3–4 Codex tasks on the same PR (security, test quality, clean code, performance), each with a focused review prompt, and merge their comments. The common-sense triage agent gets a short context dump of what went wrong and decides: retry with a hint, reassign to a different stage, or label stage/human.

The orchestrator itself is tiny. A loop that runs every 30–60 seconds, calls the GitHub/GitLab API to list issues and PRs by label, checks how many Codex threads are free, and dispatches. You could write it in Python or TypeScript in a day. The complexity lives in the prompt templates and the stage transition rules, not in the orchestrator code.

The key design decisions that actually matter:

One — how does the review fan-out work? Do you burn 4 threads on one PR (fast but blocks other work) or run one reviewer at a time (slow but keeps throughput up)? I'd start with one review agent that runs all four checklists sequentially, and only fan out when you've confirmed the per-review cost is low enough.

Two — how does the common-sense triage agent work? The simplest version: it gets the failing agent's last output, the error or review comment, and the original ticket, and its prompt says "Can you fix this in one shot? If yes, describe the fix and I'll send it back. If no, explain what a human needs to decide." That's cheap enough to run on a small model.

Three — what's the git branching model? Swarm Forge uses one worktree per role, which is elegant for local tmux agents. For Codex (which runs remotely), you'd instead use a branch naming convention: agent/refine/TICKET-123, agent/impl/TICKET-123, and the PR itself is the handoff to review.

Four — how do you prevent the agents from churning? A task that bounces between implement → review → triage → implement three times should auto-escalate to human. Put a bounce counter on the issue (a label like bounces/2 or a comment the orchestrator reads).

Want me to sketch out the actual orchestrator code and prompt templates? I'd probably prototype it as a single Python script with a GitHub Actions cron trigger, so there's zero infrastructure to run.

Thought for 28s
That clears up a lot. The synchronous-per-project constraint is the key simplification — it means the orchestrator is basically a state machine per ticket, not a scheduler.

Let me restate the design with your corrections:

Per project, one ticket at a time flows through: PBI → Refine (writer, using /debate) → Branch + Implement (writer) → Open PR → Review (N readers in parallel: security, tests, clean code, perf) → Writer resolves comments → Re-review if needed → Merge.

Failure at any stage triggers escalation, not retry at the same tier. Luna hits a wall → Terra reads the context and gives Luna a triage note → Luna retries with that context. Still stuck → Sol triages → Luna retries. Then Astra. Then human. The writer is always Luna — it's the triage quality that scales up, not the implementation model. That's clever because it keeps the expensive calls short (triage is a small context window) and the cheap calls long (implementation is where tokens burn).

The bound on token burn is structural. One branch at a time per project means one implementation running at a time. Readers are cheap because they're read-only and scoped. And the escalation chain caps retries at 4 before human review.

Let me build you a dashboard that shows the pipeline state, agent activity, and escalation chain per task.

Created a file, read a file
The dashboard shows the full pipeline with sample data from your webapi project. Click any task card to expand it and see the pipeline progress, reviewer results, escalation chain, and activity log.

A few things worth noting about the design:

The sample data shows the system in a realistic mid-day state. PBI-142 is in review with one reviewer still running and one failure. PBI-139 has been escalated to tier 1 (Terra triaged the null-handling issue, Luna retried successfully, but now has new review failures to resolve). PBI-145 is mid-implementation. PBI-131 already merged clean. Two more sit in the backlog waiting.

The escalation chain visualizes your Luna → Terra → Sol → Astra → Human path with a progress indicator per task. The triage notes show up inline so you can see exactly what context each tier gave to Luna.

The agent threads panel separates writers from readers, since that maps to your "1 writer per project, N readers" constraint. Writers are always Luna. Readers are the specialized review agents.

This is static sample data right now. The real version would poll the git provider's API and update. Want me to wire it up to GitHub's API next, or iterate on the layout first?


Swarm dashboard
Code·JSX
two things to clarify: a git *project* can affect multiple *repos*, there's one writer per *repo* not for the whole project, and once a triage problem is resolved, we need to drop back down to terra if we later encounter another problem, otherwise we run into the risk that if we encounter 4 simple problems that terra can resolve, we still escalate to human review

Thought for 6s
Good catches, both are important.

On the escalation reset — so the tier only climbs on consecutive failures. Luna fails → Terra triages → Luna succeeds → the counter drops back to 0. Next problem, Terra triages again. The tier only reaches Sol if Terra's triage didn't help and Luna fails a second time in a row on the same problem. That keeps expensive tiers for genuinely hard problems, not just frequent ones.

On the project/repo split — a project is the unit of work (a feature, a PBI), and it can touch N repos. Each repo gets its own writer. So a single PBI might have a writer working in the API repo and another writer working in the frontend repo, in parallel. The readers still fan out per PR.

Let me update the dashboard to show both.

Claude’s response was interrupted.



Message collapsed
Thought for 4s
Got it. So the hierarchy is:

Project → Repo (1 writer each) → PBI → optional Subtasks

The project is just the grouping. The repo is the unit that gets a writer. A project like "planning" could have three repos with three writers all running in parallel, each working through their own PBI queue synchronously.

Let me rebuild the dashboard around that structure.

Created 2 files, ran a command, read a file
The dashboard now reflects the real hierarchy. The sample data shows your "Planning" project with three repos (dod-guard, Pensieve, spatial-wires), each with its own writer.

The key thing to notice in the sample data: SW-31 in spatial-wires demonstrates the escalation reset. Terra triaged the connection leak problem, Luna fixed it, the counter reset to 0. Then a new problem came up (hardcoded pool size), so it goes back to Terra — not Sol. Terra gets another shot because it's a fresh problem, not the same one failing twice.

The sample data uses your actual project names from dod-guard and Pensieve. Click any PBI to expand it and see the pipeline, reviewer results, escalation history, and activity log. Each repo header shows its writer status at a glance.


Swarm dashboard
Code·JSX
1788859297332_image.png
what project template should i use

Thought for 8s
FastAPI. The orchestrator needs to serve the dashboard and expose endpoints for live state. FastAPI is async-native, which fits the polling/webhook loop naturally. Pure Python would work for the orchestrator itself, but you'd end up adding a web framework anyway the moment the dashboard needs data.
