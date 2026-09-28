# BeeHAIve live autonomous workflow

**Operator:** Open `/dashboard?project=<owner>:<number>` in Edge or another
Chromium-compatible browser and select a claimable PBI from the live Project
queue.

**BeeHAIve:** The dashboard reads the allowlisted Project state from the
server. Choose **Run full lifecycle** and approve the action. The server claims
the PBI, creates its leased workspace and branch, and starts one background
run.

**BeeHAIve:** The run executes the configured stages in order:
`refine-backlog-item`, `next-ticket`, `submit-draft-pr`, `review-pr-branch`,
`fix-pr-review`, and `complete-pr`. Each stage receives the previous stage's
JSON handover. The run inspector keeps the current stage, handoffs, checks,
questions, commit, push, and blocker evidence visible in the dashboard.

**Operator:** Follow the live status and action log. Treat a run as complete
only after the dashboard records the commit SHA and push result. If a stage
fails or waits for input, read the recorded failure or question and resolve it
through the configured operator path before continuing.
