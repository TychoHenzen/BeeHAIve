# Moving the dod-guard lifecycle onto Archon

Status: 2026-11-21. PR #306 merged (D1). Steps 1 and 2 done on BeeHAIve.

## The short version

Do not port dod-guard skill by skill. Archon's bundled SDLC pack already does
triage, planning, building, review and merging, with the gates in scripts instead
of in prose. The plan is a cutover: set the pack up on BeeHAIve, prove it on one
real issue, port only what that trial shows is missing, then retire the dod-guard
lifecycle skills.

## How dod-guard maps onto the pack

| dod-guard step | Archon workflow | What happens to the dod-guard piece |
|---|---|---|
| add-backlog-idea | archon-intake (triage on issue events) | Drop. Issues are written by hand or filed by build-pick. |
| refine-backlog-item, step plans, the seven records | archon-triage, archon-plan, archon-split | Drop. Plans become run artifacts instead of issue comments. |
| next-ticket, step-by-step | archon-build-queue, which runs archon-deliver | Drop. |
| submit-draft-pr | archon-deliver (opens and updates the draft PR) | Drop. |
| review-pr, its four judges, fix-pr-review | archon-review lenses, with three verifiers that demote weak findings | Drop. |
| Handoff comment and acceptance matrix | The review comment stamps the commit it reviewed and the contract coverage; Closes or Refs follows from it | Drop unless the trial shows a gap. |
| complete-pr, closure.mjs | archon-merge-queue and its merge gate script | Drop. |
| goal-sdlc, select-next | archon-build-pick plus the attention queue | Drop. |
| Friction log | archon-discoveries | Drop. |
| Project board Status | Nothing in the pack | Decision D3. |
| OpenSpec spec merge | Nothing in the pack | Not needed for BeeHAIve, which has no openspec folder. Revisit per repo. |
| quality-guard scan | The project's own checks | Keep as a CI check or a command in AGENTS.md, not as a workflow step. |
| dod-implement-ticket (the trial workflow in ~/.archon) | Duplicates archon-deliver | Retire after the trial. |

Rule for anything else that wants porting: it must name a real run it would have
saved, or stand in front of an irreversible step. Most of dod-guard's prose gates
fail that test, and they are the part that did not work.

## Blockers to clear first

1. **Config must be on master on GitHub.** The pack reads
   `.archon/sdlc/policy/maintainers.json` and `.archon/packs/sdlc.yaml` from the
   remote default branch only. Master has eight required checks, so this lands as a
   small PR merged to master.
2. **The trial issue must not depend on `claude/run-viewer`.** Pack runs cut their
   worktree from master. PR #306 (the run-viewer rebuild) is open against master.
   Either #306 merges first, or the trial issue touches only code already on master.
3. **AGENTS.md says `/complete-pr` alone merges.** Adopting the pack changes who
   merges. The delivery section of AGENTS.md gets rewritten in the same config PR.
4. **The GitHub forge plugin is not installed.** Install it and check:
   `archon plugin add coleam00/Archon/plugins/forge-github`, then `archon doctor`
   and `archon validate workflows`.

## Steps

1. **Setup (no tracker writes).** Install the forge plugin. Run doctor and validate.
   Check the forge operations the pack lists.
2. **Config PR to master.** Add:
   - `maintainers.json`: `["TychoHenzen"]`, the login the gh credential resolves to.
   - `sdlc.yaml`: `ci: forge` (master has real required checks), `build_lane: manual`,
     `merge_lane: manual`, `protected_paths: ".archon/**,.github/workflows/**"`.
   - The AGENTS.md delivery section, rewritten to describe the pack's queues.
   Merge it once checks pass.
3. **Trial on one issue, all by hand, manual lanes.** Write one small issue against
   master code with problem, desired outcome, invariants and acceptance. Run intake,
   then build-queue, then merge-queue, each with `--detach` and `workflow wait`.
   Answer the attention items by hand.
4. **Force one failed round.** In the same trial or a second one, make the first
   build round fail its checks on purpose, so the retry path is exercised. It was
   never exercised in the earlier trial.
5. **Gap review.** Compare the trial PR against what dod-guard would have produced.
   Port only the gaps that pass the rule above. Expected candidate: the board
   Status mirror, if D3 keeps it.
6. **Turn the lanes up.** After a few clean rounds, set `build_lane` and
   `merge_lane` to `auto`. Bind intake to issue events and the merge queue to a
   schedule. Schedules need `archon serve` running on this machine.
7. **Retire dod-guard's lifecycle skills.** Remove add-backlog-idea,
   refine-backlog-item, next-ticket, submit-draft-pr, review-pr, fix-pr-review,
   complete-pr, goal-sdlc, quick-pbi and step-by-step. Keep the skills unrelated
   to the lifecycle (blind-rewrite, clean-house, doc-reconcile and the like).
   Publish dod-guard.
8. **Other repos.** Repeat steps 1 to 3 per repository. Set `ci: none` only where a
   repo has no CI.

## Decisions for the owner

- **D1. Trial base.** (a) Merge #306 first, then trial on anything. (b) Trial now on
  an issue that touches only master code. Recommend (a) if #306 is close to done,
  because every later issue will touch run-viewer code anyway.
- **D2. Lanes for the trial.** Manual lanes mean every build and merge waits for your
  answer. Auto lanes mean the pack acts on clear cases alone. Recommend manual for
  the trial, auto after a few clean rounds.
- **D3. Project board.** (a) Drop the board, and use the pack's labels and
  `archon workflow attention` as the queue. (b) Keep it with GitHub Projects'
  built-in workflows (auto-add, Done on close). (c) Port one script that mirrors
  each work item's stage to Status. Recommend (b): no code, and the board still
  shows Done.
- **D4. BeeHAIve's purpose.** BeeHAIve reads dod-guard's transcript habits, such as
  the "Stop reason:" line and the skill named in the first prompt. Once dod-guard
  retires, those habits stop. Archon has its own run console (`archon serve`).
  (a) Retarget BeeHAIve at Archon runs. (b) Freeze it. (c) Keep it for plain Claude
  Code sessions. This also decides whether PR #311 is still worth merging. Your call;
  it depends on what you want BeeHAIve for.
- **D5. Telemetry.** Archon sends anonymous usage data by default. Setting
  `DO_NOT_TRACK=1` turns it off. Recommend turning it off.
- **D6. Where custom workflows live.** Anything ported goes in the repo's
  `.archon/workflows/` (versioned, reviewed by the pack's protected paths), not only
  in `~/.archon`.

## Cutover rule for work already in flight

Finish #306 and #311 the old way. Start new issues under Archon. Never run both
systems on one issue.
