# Agent Swarms for Cheap, High-Volume, Continuous LLM Work: Frameworks Compared (2025–2026)

## TL;DR

> Scope correction: this ticket evaluates third-party orchestration around hosted Codex versus a direct `codex exec` wrapper. Local-only inference is not a requirement.
- **Swarm Forge is a specialized pipeline reference, not the current default.** Its role-based handoffs and worktree isolation are relevant design patterns, but the BeeHAIve decision is whether that complexity beats a direct Codex wrapper.
- The generic cheap-call framework survey below is background only. The current decision compares a third-party orchestrator around Codex with a direct `codex exec` wrapper.
- **The key tradeoff is integration cost versus coordination value.** A third-party framework must improve quality, throughput, recovery, or provider flexibility enough to repay its adapter, dependency, state, and operational burden.

## Key Findings

## Decision framing for BeeHAIve

[Issue #12](https://github.com/TychoHenzen/BeeHAIve/issues/12) asks whether BeeHAIve should put a third-party orchestrator at the center of its workflow or wrap Codex execution directly. Hosted Codex is in scope. Local inference is not a requirement for this decision.

The relevant baseline is a thin wrapper around `codex exec`. The wrapper can own ticket intake, worktree isolation, concurrency, timeouts, retries, result storage, and GitHub delivery. Codex can continue to own model execution, repository tools, sandboxing, skills, and approvals.

The third-party option is justified only if it supplies a measured capability the wrapper lacks, such as durable cross-task graph state, fan-out and fan-in, recovery after process loss, or a provider-independent execution layer. If its nodes launch `codex exec`, the framework becomes a second lifecycle and state machine around the Codex harness. If it calls the OpenAI API directly, it measures a different agent harness.

## Integration surfaces

The [Codex CLI documentation](https://developers.openai.com/codex/cli) explicitly supports calling `codex exec` from repeatable workflows and pipelines. The local CLI run used here was version 0.151.0 and emitted JSONL lifecycle and usage events.

The [Codex SDK documentation](https://developers.openai.com/codex/sdk) provides a first-party upgrade path. Its TypeScript and Python libraries can start, continue, and resume local Codex threads. The [app server](https://developers.openai.com/codex/app-server) exposes the Codex lifecycle over JSON-RPC and streams thread, turn, command, and approval events.

LangGraph is a low-level graph runtime. Its [graph API](https://docs.langchain.com/oss/python/langgraph/graph-api) supports parallel outgoing edges and bounded concurrency. Its [persistence layer](https://docs.langchain.com/oss/python/langgraph/persistence) supports checkpoints. CrewAI [Flows](https://docs.crewai.com/v1.15.20/en/concepts/flows) and Microsoft Agent Framework [workflows](https://learn.microsoft.com/en-us/agent-framework/overview/) provide comparable application-level state and routing abstractions.

A Python 3.13 dependency dry-run resolved 23 packages for LangGraph 1.2.11 and 112 packages for CrewAI 1.15.20. The first-party `openai-codex` SDK resolved two packages. These counts measure dependency footprint, not engineering time. The direct wrapper adds no Python framework dependency.

| Option | It adds | BeeHAIve still owns | Integration risk |
|---|---|---|---|
| Direct `codex exec` wrapper | Process supervision and JSONL parsing | Scheduling, worktrees, retries, artifacts, and delivery | Lowest. It follows the Codex execution boundary. |
| LangGraph around Codex | Graph state, routing, fan-out, and checkpoints | A worker adapter plus the same scheduling and artifact policies | Medium. Two runtimes must agree on retries, state, and failures. |
| CrewAI Flow around Codex | Event-driven flow and role abstractions | A worker adapter plus Codex lifecycle and artifact policies | High. The dependency surface is larger, with no proven benefit yet. |
| Microsoft Agent Framework around Codex | Graph workflows and checkpointing | A provider or process adapter plus Codex lifecycle policies | High. It is a second workflow runtime to operate. |

This table separates framework capability from integration benefit. A framework feature is not a benefit until the benchmark shows that the direct wrapper cannot deliver the same outcome within its operational limits.

1. **Swarm Forge is real, authored by Robert C. "Uncle Bob" Martin, and philosophically distinctive.** It embeds TDD, mutation testing, and complexity control as a machine-readable "Constitution" every agent must obey, using git worktrees for isolation and tmux for observability. It is deliberately anti-framework: shell scripts and Clojure/Babashka, not cloud infrastructure.

2. **Swarm Forge's coordination is a pipeline of specialized roles with durable, committed handoffs**, not emergent swarm intelligence. It is centralized: a config file defines a fixed topology, and roles hand off in order.

3. **No mainstream framework is purpose-built around "many cheap models in parallel for continuous work."** The frameworks split into (a) orchestration libraries (LangGraph, CrewAI, AutoGen/MAF, OpenAI Agents SDK, Swarms, ROMA) and (b) execution/scaling substrates (Ray/Anyscale, SWE-ReX, tmux+worktree tools). The cheap-swarm strategy is achieved by *combining* a token-efficient orchestrator, cheap models with a routing layer, and a scaling substrate.

4. **Token efficiency varies dramatically and directly drives cost at high volume.** LangGraph's explicit graph structure is consistently the most token-efficient/cheapest per task; CrewAI adds meaningful overhead; AutoGen's conversational model is the most token-heavy and the biggest cost risk.

5. **Continuous/long-running work requires durable execution (checkpointing).** LangGraph and Microsoft Agent Framework have first-class checkpointing that survives crashes and multi-day pauses; CrewAI's persistence is lighter (SQLite-backed Flow state); OpenAI Swarm has none.

## Background framework notes

### 1. Uncle Bob's Swarm Forge

**What it is.** Swarm Forge (`github.com/unclebob/swarm-forge`) is described by its author as "a simple tool for coordinating several AI agents." It is written largely in Clojure/Babashka plus shell scripts and has grown to roughly 3,800 GitHub stars with 364 forks. It was developed publicly through Uncle Bob's "Clean AI: Agentic Discipline" series on cleancoders.com (with Justin Martin). Its README opens with a warning that there is no associated crypto token ("Do not spend any money on a bankrbot SWARM token").

**Philosophy.** Martin's long-standing thesis is that software quality comes from process discipline, not individual brilliance. Swarm Forge applies this to agents: a model will happily write code with no tests, restructure something it shouldn't have touched, and report success — so the system forces professional discipline via fixed roles, mandatory handoffs, and a layered "Constitution" of engineering rules.

**Architecture.**
- **Substrate**: a single orchestrator (`swarmforge.sh` / `swarmforge.bb`) reads a config, initializes git, creates one git worktree per role under `.worktrees/`, launches one tmux session per role, and starts a handoff daemon and local dashboard.
- **Config-driven topology**: the swarm shape comes from `swarmforge/swarmforge.conf`, each line being `window <role> <backend> <worktree> …`. Exactly one role uses the `master` worktree (the project's main checkout). Other roles get isolated `.worktrees/<name>` checkouts.
- **Isolation**: each agent works only in its own git worktree/branch; work flows between agents through explicit, committed merges (durable handoffs via `swarm_handoff.sh`, `ready_for_next.sh`, `done_with_current.sh`) rather than shared memory.
- **Constitution**: `constitution.prompt` delegates to layered article files (project → engineering → workflow, with earlier files winning on conflict). Shared articles (engineering, workflow, handoffs) come from the `main` branch; product "packs" specialize them with local additions.
- **Observability & control**: a dashboard (and an optional Logger role that tails `logs/agent_messages.log`) lets an operator start work, inspect agents, handle approval gates, answer clarifications, and stop the swarm.
- **Backends**: per-role choice of `claude`, `codex`, `grok`, `copilot`, or `none`.

**Products/topologies** (installed via `get-swarm-forge`): `two-pack` (coder → cleaner), `four-pack` (specifier → coder → refactorer → architect), `six-pack` (six roles: specification, implementation, cleanup, architecture, hardening, QA), plus multi-project "forge" variants (`project-manager`, `lieutenant`) with a planning "lieutenant." Martin reported building a Missile Command remake in Clojure/ClojureScript in a single day with a six-pack swarm.

**Fit for the user's goal.** Swarm Forge is optimized for *disciplined quality* using a *small number of premium CLI coding agents*, not for high-volume cheap API calls. It has no built-in model-cost routing, no cheap-model escalation logic, and its parallelism is bounded by the number of roles and one machine's CPU/RAM/disk. Its handoff protocol and machine-readable "constitution" are, however, an excellent design template for anyone building a continuous, autonomous *coding* pipeline.

### 2. The major open-source orchestration frameworks

**LangGraph (LangChain).** Graph-based (nodes = actions, edges = transitions, shared typed state). Reached v1.0 GA on **22 October 2025** with a stability pledge of no breaking changes until 2.0, and is now standalone (no longer requires LangChain). Coordination is explicit and deterministic; it natively supports cycles, branching, retries, and a first-class human-in-the-loop `interrupt()`. Durable execution via checkpointers (e.g., PostgresSaver) that survive process restarts and multi-day pauses — the best fit for continuous/long-running work. Provider-agnostic, supporting different models per node (so you can put cheap models on cheap nodes and escalate elsewhere). Consistently the most token-efficient in independent benchmarks and the cheapest per task, with the lowest memory footprint. Steepest learning curve. Enterprises including JPMorgan, Klarna, and BlackRock reportedly run it in production. **Best all-round pick for cost-efficient, continuous, high-volume orchestration.**

**CrewAI.** Role-based ("crew" of agents with roles, goals, tools); passed v1.0 and is at the 1.15.x line with 50k+ stars. Coordination via sequential or hierarchical processes; the newer **Flows** layer adds event-driven, state-machine control (`@start`/`@listen`/`@router`). Fastest on-ramp (a working crew in an afternoon). Per multiple 2026 benchmarks — e.g., Kim Jangwook's *AI Agent Framework Comparison 2026* — CrewAI "consumes about 18% more tokens…from the role/backstory definitions," while showing "40% faster development time versus LangGraph, and a 45s vs 68s latency advantage on the same 5-step research workflow"; other benchmarks report up to ~3× the token footprint of LangGraph on simple single-tool-call flows. Hierarchical mode costs ~30% more than sequential. Persistence is lighter (SQLite-backed Flow state). Good for medium workloads and role-shaped problems; less robust for SLA-critical continuous work.

**AutoGen / AG2 / Microsoft Agent Framework.** AutoGen pioneered conversational multi-agent orchestration (asynchronous, event-driven, code execution). It is now **in maintenance mode** — its GitHub README states: "⚠️ Maintenance Mode. AutoGen is now in maintenance mode… New users should start with Microsoft Agent Framework." The official successor, the **Microsoft Agent Framework (MAF)**, a merger of AutoGen and Semantic Kernel, reached **1.0 GA on 3 April 2026** for .NET and Python ("Microsoft Agent Framework has reached version 1.0… the production-ready release"). MAF adds a stable graph-based workflow engine ("branch on conditions, fan out to parallel steps, and converge results"), durable execution ("Checkpointing and hydration ensure long-running processes survive interruptions"), native MCP tool discovery, YAML declarative agents/workflows, and multi-provider model support (Azure OpenAI, OpenAI, Anthropic, Bedrock, Gemini, and local models via Ollama/ONNX) — so cheaper/local models can be assigned per agent. A2A (agent-to-agent) protocol support was flagged "A2A 1.0 support coming soon" at GA. A separate community fork, **AG2** (maintained by original AutoGen creators Chi Wang and Qingyun Wu under the AG2AI org, Apache-2.0, actively developed — v0.12.2 released May 2026), inherited the original PyPI packages and Discord. AutoGen's conversational model is the most token-heavy (3–5× LangGraph on some tasks) and the biggest cost risk at high volume — always cap `max_consecutive_auto_reply` and set a per-conversation token ceiling.

**OpenAI Swarm / Agents SDK.** OpenAI Swarm (Oct 2024) was a deliberately minimal (<1000 lines), stateless, client-side *educational* framework built on Chat Completions, using two primitives: handoffs (an agent-returning function switches control) and context variables. It reached 20k+ stars but is **deprecated**; the README redirects to the **OpenAI Agents SDK** ("Swarm is now replaced by the OpenAI Agents SDK, which is a production-ready evolution of Swarm"). The Agents SDK (shipped March 2025, v0.17.1 by May 2026, 26k+ stars) adds guardrails, tracing, sessions, and hosted tools, and works with non-OpenAI models via LiteLLM/gateways. Swarm has no persistence, no monitoring, and no robust error handling — do not use it in production; keep it for learning handoff concepts only.

**Swarms (kyegomez).** Enterprise-oriented framework with 60+ prebuilt multi-agent structures: sequential, concurrent, hierarchical, mesh, MixtureOfAgents, MajorityVoting, councils, plus routers/selectors (SwarmRouter, ModelRouter, AuctionSwarm) that pick which agent/model handles a task — the selector can be an LLM "boss," an embedding match, a skill-graph lookup, or an agent's own bid. Backward-compatible with LangChain/AutoGen/CrewAI; MCP support; SwarmRouter adds two-tier caching to cut initialization overhead. It explicitly markets "orchestrating millions of agents" and is a strong foundation when you need *configurable* orchestration patterns and model routing for cost. Note it is a single-maintainer-led project; evaluate maturity for your use case.

**ROMA (Sentient AI).** Recursive Open Meta-Agent (open-sourced Sept 2025, ~3.4–5k stars, arXiv:2602.01848). Four modular roles — Atomizer, Planner, Executor, Aggregator — recursively decompose a goal into a dependency-aware subtask tree, execute parallelizable subtasks simultaneously, and aggregate/compress intermediate results to control context growth. Designed specifically for *long-horizon* multi-agent work with parallelism, and it cleanly separates orchestration from model selection (so you can plug in cheap executors). Beta; moderate-to-high learning curve; enforce depth/branch limits.

### 3. Scaling substrates for "many cheap calls"

- **Ray / Anyscale**: the distributed-computing layer OpenAI uses to coordinate training of ChatGPT. Anyscale has published "massively parallel agentic simulations" running thousands of agents in parallel, each agent a lightweight Ray actor, with Ray Serve + vLLM providing high-throughput inference on a single cluster (avoiding rate limits). This is the go-to substrate when you genuinely need tens of thousands of concurrent cheap model calls. Research frameworks like **Matrix** (peer-to-peer, decentralized, on Ray) scale to tens of thousands of concurrent agentic workflows by eliminating the central-orchestrator bottleneck; agents handle control/data flow as serialized messages in distributed queues while heavy inference is offloaded to Ray Serve/vLLM/SGLang.
- **tmux + git-worktree tooling** (dmux, ccmanager, polydev, Superset, plus Uncle Bob's Swarm Forge): the pragmatic local approach for parallel *coding* agents. Cheap when tied to a flat-rate subscription (e.g., Claude Code Max) rather than per-token API billing. Practitioner consensus: excellent for 3–5 parallel agents on independent tasks; coordination overhead and shared-machine resource contention dominate beyond that.
- **SWE-ReX**: a sandboxed, massively-parallel code-execution runtime that runs many agents locally or in the cloud (powers SWE-agent).

### 4. Architecture styles at a glance

- **Centralized / orchestrator-worker**: OpenAI Agents SDK, CrewAI (hierarchical), Swarms (HierarchicalSwarm), ROMA, Anthropic's Research system. A smart lead model plans and delegates to cheaper workers — the canonical "smart orchestrator + cheap sub-agents" cost pattern. Bottleneck: the orchestrator is a single point of failure and a throughput/context ceiling.
- **Graph / state-machine**: LangGraph, Microsoft Agent Framework, CrewAI Flows. Deterministic, token-efficient, durable — best for continuous work.
- **Decentralized / peer-to-peer / mesh**: Matrix and true "swarm" topologies where coordination emerges from local rules and shared state. Best raw scalability; hardest to debug.
- **Pipeline with durable handoffs**: Swarm Forge. Fixed role order, committed handoffs, worktree isolation.

### 5. The cost-efficiency verdict on "cheap swarm vs. one expensive model"

The evidence is genuinely mixed and hinges on task value:
- **Pro-swarm**: Per Anthropic's June 2025 engineering post *"How we built our multi-agent research system,"* a system with Claude Opus 4 as lead and Claude Sonnet 4 subagents "outperformed single-agent Claude Opus 4 by 90.2% on our internal research eval," with "token usage by itself explaining 80% of the variance," while multi-agent systems used "about 15× more tokens than chats." Anthropic's conclusion: it is worth it only when task value exceeds token cost (legal due diligence, competitive intelligence, biomedical review), and the published architecture has no built-in circuit breakers, so a runaway sub-agent can multiply cost further.
- **Anti-swarm**: Jwalapuram, Lin, Ke et al., *"The Illusion of Multi-Agent Advantage"* (arXiv:2606.13003, submitted 11 June 2026; NTU, Meta AI, Oxford, Tokyo Tech) found that "automatic MAS consistently underperform CoT-SC despite being up to 10× more expensive," tested on BrowseComp-Plus and reasoning datasets. Separately, Cemri, Pan, Yang et al., *"Why Do Multi-Agent LLM Systems Fail?"* (arXiv:2503.13657, NeurIPS 2025) built the Multi-Agent System Failure Taxonomy (MAST) from 150+ traces (inter-annotator κ=0.88), identifying "14 unique modes, clustered into 3 categories: (i) system design issues, (ii) inter-agent misalignment, and (iii) task verification." Multi-agent cost also compounds nonlinearly: budget for 3× and the bill often lands at 5–15× because context is re-billed on every handoff.
- **The synthesis (LLM–SLM orchestration)**: The winning 2026 production pattern is not "small instead of large" but routing each step to the smallest model that provably clears an accuracy bar, escalating only low-confidence cases to a frontier model (a cheap semantic router adds ~50–100 ms). Documented savings are large:
  - NVIDIA Research (Belcak et al., *"Small Language Models are the Future of Agentic AI,"* arXiv:2506.02153, June 2025): "Serving a 7bn SLM is 10–30× cheaper (in latency, energy consumption, and FLOPs) than a 70–175bn LLM, enabling real-time agentic responses at scale."
  - Gandhi, Patwardhan, Vig & Shroff (TCS Research), *"BudgetMLAgent"* (arXiv:2411.07464): a "94.2% reduction in the cost (from $0.931 per run…for GPT-4 single agent system to $0.054)," while *raising* success rate to 32.95% vs 22.72% on MLAgentBench by using a low-cost model for most calls and escalating on failure.
  - Harness-adapted small-model agents can be ~90% cheaper at on-par performance on routine business tasks, and agentic plan caching cuts average serving cost ~46% while retaining ~97% accuracy.

### 6. Codex Luna rerun for issue #12

The earlier Ollama result is not a valid quality conclusion. I reran the same fixed workload with Codex CLI 0.151.0 and model `gpt-5.6-luna` in an isolated, read-only directory on 8 September 2026. The workload had four labeled cases: a documentation-only change, a tested parser refactor, an untested payment-processing change, and a private-variable rename. The expected labels were `SIMPLE`, `COMPLEX`, `REVIEW`, and `SIMPLE`.

| Topology | Repetitions | Correct | Median wall time | Usage per four-case run |
|---|---:|---:|---:|---:|
| One Luna call with all four cases | 7 | 28/28 (100%) | 9.34 s* | about 23.5k input and 76-93 output tokens |
| Four independent Luna calls in parallel | 3 batches | 12/12 (100%) | 10.17 s** | about 93.6k input and 211-278 output tokens |

\* Median of the three final isolated repetitions. ** Median of the slowest call in each parallel batch. The CLI usage stream reported variable prompt-cache hits. At the published Luna API rates, the combined call is about $0.0048 per run and the fan-out batch is about $0.0122-$0.0162. These are API-equivalent estimates, not a Codex subscription invoice. The [official Luna model page](https://developers.openai.com/api/docs/models/gpt-5.6-luna) lists the model ID, structured-output support, and $0.20 input/$1.20 output pricing.

Luna produced the same perfect accuracy with both topologies. Fan-out added roughly four times the input tokens and 2.5-3.4 times the API-equivalent cost, while the observed parallel wall time was about 9% slower. Local CPU and RAM use cannot be measured for this hosted model.

This fixture is saturated. It is useful as a protocol sanity check, but it cannot show an accuracy improvement because the single-call baseline already reached 100%. It therefore provides no topology decision. The earlier Ollama signal is also not a usable quality reference. A harder, multi-step fixture with room below the accuracy bar is required before selecting a default.

The next fixture must persist raw outputs, score partial findings, and measure the integration burden as well as quality, throughput, latency, retries, and recovery. The central comparison is a direct Codex wrapper versus a third-party graph that uses the same Codex model and task permissions.

## Benchmark protocol for the final topology decision

1. Build at least ten real multi-step repository tasks from the target workflow. Each task must start from a clean repository state and include hidden tests or a deterministic acceptance check. Do not include tasks whose expected answer is stated directly in the prompt.
2. Run the direct wrapper and each included third-party candidate with the same `gpt-5.6-luna` model, prompt content, repository state, sandbox permissions, and maximum retry budget. A third-party candidate must invoke the same Codex worker or be labeled as a different harness comparison.
3. Repeat every task at least three times. Preserve each candidate's JSONL output, exit status, changed-file diff, test output, token usage, start and end timestamps, retry count, and failure reason.
4. Score hidden-test or acceptance success, required finding recall, false-positive rate, and artifact validity. Summarize medians and failure distributions instead of reporting only a mean or a best run.
5. Record integration effort separately: dependency count, adapter lines, configuration steps, state-store requirements, recovery behavior, and operator-visible failure handling. Record peak process and memory use when the runner can observe them.
6. Adopt the third-party option only when it clears the declared quality, throughput, or recovery threshold and its measured benefit repays the adapter and operational burden. Otherwise keep the direct wrapper and record the framework as evaluated but not selected.

## Recommendations

**For BeeHAIve's current decision:**

1. **Start with a thin `codex exec` wrapper.** Keep ticket intake, worktree creation, bounded concurrency, timeouts, retries, raw event capture, and GitHub delivery outside the model.
2. **Use the Codex SDK instead of a third-party framework only when the wrapper reaches a real process boundary.** The official SDK supports starting, continuing, and resuming local Codex threads. The app server exposes streamed JSON-RPC events when the application needs finer control.
3. **Evaluate LangGraph first if the wrapper cannot express the needed workflow.** Its graph API supplies explicit parallel branches and bounded concurrency. Its persistence layer supplies checkpointing. Both capabilities matter only if BeeHAIve needs application-owned state across Codex jobs.
4. **Treat CrewAI and Microsoft Agent Framework as alternatives, not defaults.** They can model event-driven or graph workflows, but each still needs an adapter and an ownership decision for Codex state, approvals, retries, and artifacts.
5. **Keep hard limits in the wrapper or graph.** Set per-job timeouts, concurrency caps, retry budgets, token budgets, and a circuit breaker for recursive delegation.

**Staged rollout and thresholds:**
- **Wrapper spike:** implement one `codex exec --json` job, one isolated worktree, one timeout, and one persisted result record. Keep this small enough to inspect and replace.
- **Third-party spike:** implement the same job in one graph framework, using Codex as the worker. Record adapter code, dependencies, setup steps, and failure handling.
- **Adopt the framework only if** it delivers at least a 10 percentage-point quality gain, 2x throughput at equal quality, or a required recovery or fan-out capability that the wrapper cannot provide within the agreed operational limits.
- **Keep the wrapper** when the gain is below those thresholds or when the framework duplicates Codex lifecycle behavior without improving measured outcomes.

## Caveats
- **Swarm Forge is young and idiosyncratic** (Clojure/shell, macOS-leaning Terminal integration, small maintainer base). It targets premium CLI coding agents, not cheap API-call swarms; treat it as a design exemplar, not a drop-in high-volume engine.
- **Framework benchmarks are inconsistent** and frequently vendor- or blog-authored. Specific token/cost/latency figures (e.g., "$0.08 vs $0.45 per task," "18% overhead," "62% vs 54% success") come from small, non-standardized tests and should be re-validated on your own workload before you rely on them.
- **The field is moving fast**: OpenAI Swarm → Agents SDK; AutoGen → Microsoft Agent Framework; CrewAI Flows; LangGraph 1.x. Version numbers and "maintenance mode" statuses change quarterly, and some cited capabilities (e.g., MAF's A2A 1.0 support) were flagged "coming soon" at announcement.
- **Multi-agent is not automatically better.** Peer-reviewed and industry evidence both show single well-tuned agents (or Chain-of-Thought-with-Self-Consistency) frequently beat naive multi-agent systems on cost and sometimes accuracy. The cheap-swarm strategy pays off specifically for parallelizable, breadth-first, high-value work with disciplined routing and verification — not as a universal default.
