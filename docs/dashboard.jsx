import { useState, useCallback } from "react";
import { GitBranch, GitMerge, CheckCircle2, AlertTriangle, ChevronRight, ChevronDown, ArrowRight, Circle, MessageSquare, Shield, TestTube, Gauge, Sparkles, RefreshCw, Eye, Pen, FolderGit2, Layers } from "lucide-react";

const TIERS = [
  { id: "luna", label: "Luna", color: "#6b7280", desc: "Cheapest — does the work" },
  { id: "terra", label: "Terra", color: "#3b82f6", desc: "First triage" },
  { id: "sol", label: "Sol", color: "#f59e0b", desc: "Second triage" },
  { id: "astra", label: "Astra", color: "#8b5cf6", desc: "Last resort triage" },
  { id: "human", label: "Human", color: "#ef4444", desc: "Manual review" },
];

const STAGES = [
  { id: "backlog", label: "Backlog", icon: Circle },
  { id: "refine", label: "Refine", icon: MessageSquare },
  { id: "implement", label: "Implement", icon: Pen },
  { id: "review", label: "Review", icon: Eye },
  { id: "resolve", label: "Resolve", icon: RefreshCw },
  { id: "merge", label: "Merged", icon: GitMerge },
];

const REVIEWERS = [
  { id: "security", label: "Security", icon: Shield, color: "#ef4444" },
  { id: "tests", label: "Test coverage", icon: TestTube, color: "#3b82f6" },
  { id: "clean", label: "Clean code", icon: Sparkles, color: "#8b5cf6" },
  { id: "perf", label: "Performance", icon: Gauge, color: "#f59e0b" },
];

const SAMPLE_DATA = {
  name: "Planning",
  repos: [
    {
      name: "dod-guard",
      writer: { status: "active", currentPbi: "DG-18" },
      pbis: [
        {
          id: "DG-18",
          title: "Enforce folder hierarchy and production-test separation in workflow",
          stage: "review",
          branch: "feature/DG-18-folder-hierarchy",
          escalation: { current: 0, consecutive: 0 },
          escalationLog: [],
          subtasks: [],
          reviewers: {
            security: { status: "pass", comment: null },
            tests: { status: "fail", comment: "No test for nested folder edge case where both prod and test files coexist." },
            clean: { status: "pass", comment: null },
            perf: { status: "pass", comment: null },
          },
          log: [
            { time: "09:12", agent: "luna", action: "Picked up DG-18 for refinement" },
            { time: "09:14", agent: "luna", action: "Running /debate — workflow designer vs plugin consumer vs CI engineer" },
            { time: "09:20", agent: "luna", action: "Refinement complete. Criteria: recursive scan, .test suffix convention, fail-fast on violation" },
            { time: "09:21", agent: "luna", action: "Created branch feature/DG-18-folder-hierarchy" },
            { time: "09:38", agent: "luna", action: "Implementation complete. Opened PR #42" },
            { time: "09:41", agent: "tests", action: "Fail — missing nested coexistence edge case" },
          ],
        },
        {
          id: "DG-22",
          title: "Clean Code Explorer entrypoint and support quality findings",
          stage: "backlog",
          branch: null,
          escalation: { current: 0, consecutive: 0 },
          escalationLog: [],
          subtasks: [
            { id: "DG-22a", title: "Split Code Explorer composition and support responsibilities", stage: "backlog" },
          ],
          reviewers: {},
          log: [],
        },
      ],
    },
    {
      name: "Pensieve",
      writer: { status: "active", currentPbi: "PN-07" },
      pbis: [
        {
          id: "PN-07",
          title: "Define a continuous learning path from Fashion-MNIST to LLM training",
          stage: "implement",
          branch: "feature/PN-07-learning-path",
          escalation: { current: 0, consecutive: 0 },
          escalationLog: [],
          subtasks: [],
          reviewers: {},
          log: [
            { time: "09:05", agent: "luna", action: "Picked up PN-07 for refinement" },
            { time: "09:07", agent: "luna", action: "Running /debate — ML engineer vs curriculum designer vs compute-budget realist" },
            { time: "09:15", agent: "luna", action: "Refinement complete. Path: MNIST → CIFAR → GPT-2 small → LoRA fine-tune" },
            { time: "09:16", agent: "luna", action: "Created branch feature/PN-07-learning-path" },
            { time: "09:16", agent: "luna", action: "Implementing..." },
          ],
        },
      ],
    },
    {
      name: "spatial-wires",
      writer: { status: "active", currentPbi: "SW-31" },
      pbis: [
        {
          id: "SW-31",
          title: "Add connection pooling to spatial query engine",
          stage: "resolve",
          branch: "feature/SW-31-connection-pool",
          escalation: { current: 1, consecutive: 1 },
          escalationLog: [
            { tier: "terra", resolved: true, note: "Luna's pool wasn't releasing connections on query timeout. Add a finally block in executeQuery that calls pool.release() — the timeout catch swallows the error but never cleans up." },
            { tier: "terra", resolved: false, note: "Review found the pool size is hardcoded. Luna needs to read it from SpatialConfig.ConnectionPool.MaxSize and fall back to 10." },
          ],
          subtasks: [],
          reviewers: {
            security: { status: "pass", comment: null },
            tests: { status: "pass", comment: null },
            clean: { status: "fail", comment: "Hardcoded pool size. Read from SpatialConfig." },
            perf: { status: "pass", comment: "Pool reuse is correct. 3× throughput improvement on benchmark." },
          },
          log: [
            { time: "08:20", agent: "luna", action: "Picked up SW-31 for refinement" },
            { time: "08:35", agent: "luna", action: "Implementation complete. Opened PR #115" },
            { time: "08:38", agent: "tests", action: "Fail — connections leak on timeout" },
            { time: "08:39", agent: "luna", action: "Fix attempt failed" },
            { time: "08:40", agent: "terra", action: "Triage: missing finally block in executeQuery for pool.release()" },
            { time: "08:41", agent: "luna", action: "Fix applied. Connection leak resolved ✓" },
            { time: "08:42", agent: "luna", action: "Escalation reset → 0" },
            { time: "08:45", agent: "luna", action: "Re-review started" },
            { time: "08:48", agent: "clean", action: "Fail — hardcoded pool size" },
            { time: "08:49", agent: "luna", action: "Fix attempt failed — doesn't know where config lives" },
            { time: "08:50", agent: "terra", action: "Triage: read from SpatialConfig.ConnectionPool.MaxSize, default 10" },
            { time: "08:51", agent: "luna", action: "Retrying with triage context" },
          ],
        },
        {
          id: "SW-34",
          title: "Support multi-polygon input for region queries",
          stage: "merge",
          branch: "feature/SW-34-multi-polygon",
          escalation: { current: 0, consecutive: 0 },
          escalationLog: [],
          subtasks: [],
          reviewers: {
            security: { status: "pass", comment: null },
            tests: { status: "pass", comment: null },
            clean: { status: "pass", comment: null },
            perf: { status: "pass", comment: null },
          },
          log: [
            { time: "07:30", agent: "luna", action: "Picked up SW-34" },
            { time: "07:55", agent: "luna", action: "Implementation complete. Opened PR #113" },
            { time: "08:00", agent: "system", action: "All reviewers passed. Auto-merged PR #113" },
          ],
        },
        {
          id: "SW-35",
          title: "Add GeoJSON export for query results",
          stage: "backlog",
          branch: null,
          escalation: { current: 0, consecutive: 0 },
          escalationLog: [],
          subtasks: [],
          reviewers: {},
          log: [],
        },
      ],
    },
  ],
};

function TierBadge({ tier }) {
  const t = TIERS.find((x) => x.id === tier);
  if (!t) {
    if (tier === "system") return <span className="text-xs px-1.5 py-0.5 rounded font-medium" style={{ background: "rgba(107,114,128,0.15)", color: "#9ca3af" }}>System</span>;
    return null;
  }
  return <span className="text-xs px-1.5 py-0.5 rounded font-medium" style={{ background: t.color + "22", color: t.color }}>{t.label}</span>;
}

function StatusDot({ status }) {
  const colors = { pass: "#22c55e", fail: "#ef4444", running: "#f59e0b", pending: "#6b7280" };
  const c = colors[status] || colors.pending;
  return (
    <span className="relative flex items-center justify-center w-3 h-3">
      <span className="w-2.5 h-2.5 rounded-full" style={{ background: c }} />
      {status === "running" && <span className="absolute w-3 h-3 rounded-full animate-ping opacity-40" style={{ background: c }} />}
    </span>
  );
}

function EscalationChain({ escalation, escalationLog }) {
  if (escalationLog.length === 0) return null;
  return (
    <div className="mt-3 p-2.5 rounded-lg" style={{ background: "rgba(251,191,36,0.05)", border: "1px solid rgba(251,191,36,0.12)" }}>
      <div className="flex items-center gap-2 mb-2">
        <AlertTriangle size={13} style={{ color: "#f59e0b" }} />
        <span className="text-xs font-medium" style={{ color: "#f59e0b" }}>
          Consecutive failures: {escalation.consecutive}
        </span>
      </div>
      <div className="space-y-1.5">
        {escalationLog.map((entry, i) => (
          <div key={i} className="flex items-start gap-2 text-xs">
            {entry.resolved
              ? <CheckCircle2 size={12} style={{ color: "#22c55e", marginTop: 1, flexShrink: 0 }} />
              : <Circle size={12} style={{ color: "#f59e0b", marginTop: 1, flexShrink: 0 }} />}
            <TierBadge tier={entry.tier} />
            <span style={{ color: "#9ca3af" }}>{entry.note}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function ReviewPanel({ reviewers }) {
  if (!reviewers || Object.keys(reviewers).length === 0) return null;
  return (
    <div className="mt-3 space-y-1.5">
      <div className="text-xs font-medium" style={{ color: "#9ca3af" }}>Reviewers</div>
      {REVIEWERS.map((r) => {
        const rev = reviewers[r.id];
        if (!rev) return null;
        const Icon = r.icon;
        return (
          <div key={r.id} className="flex items-start gap-2 text-xs">
            <StatusDot status={rev.status} />
            <Icon size={13} style={{ color: r.color, marginTop: 1, flexShrink: 0 }} />
            <span style={{ color: "#e5e7eb" }}>{r.label}</span>
            {rev.comment && <span className="ml-1" style={{ color: "#9ca3af" }}>— {rev.comment}</span>}
          </div>
        );
      })}
    </div>
  );
}

function PbiCard({ pbi, expanded, onToggle }) {
  const stageInfo = STAGES.find((s) => s.id === pbi.stage);
  const isActive = ["refine", "implement", "review", "resolve"].includes(pbi.stage);
  const isDone = pbi.stage === "merge";

  return (
    <div
      className="rounded-lg p-3 mb-1.5 transition-all cursor-pointer"
      style={{ background: "#1a2332", border: `1px solid ${expanded ? "rgba(59,130,246,0.4)" : "transparent"}` }}
      onClick={onToggle}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-0.5 flex-wrap">
            <span className="text-xs font-mono font-medium" style={{ color: "#60a5fa" }}>{pbi.id}</span>
            <span className="text-xs px-1.5 py-0.5 rounded-full" style={{
              background: isDone ? "rgba(34,197,94,0.12)" : isActive ? "rgba(59,130,246,0.12)" : "#374151",
              color: isDone ? "#22c55e" : isActive ? "#60a5fa" : "#6b7280",
            }}>
              {stageInfo?.label}
            </span>
            {pbi.escalation.consecutive > 0 && (
              <span className="flex items-center gap-1 text-xs" style={{ color: "#f59e0b" }}>
                <AlertTriangle size={10} /> ×{pbi.escalation.consecutive}
              </span>
            )}
          </div>
          <div className="text-sm" style={{ color: "#e5e7eb" }}>{pbi.title}</div>
          {pbi.branch && (
            <div className="flex items-center gap-1.5 mt-1 text-xs" style={{ color: "#6b7280" }}>
              <GitBranch size={10} />
              <span className="font-mono">{pbi.branch}</span>
            </div>
          )}
          {pbi.subtasks.length > 0 && (
            <div className="mt-1.5 ml-3 space-y-0.5">
              {pbi.subtasks.map((st) => (
                <div key={st.id} className="flex items-center gap-2 text-xs" style={{ color: "#9ca3af" }}>
                  <Layers size={9} />
                  <span className="font-mono" style={{ color: "#60a5fa" }}>{st.id}</span>
                  {st.title}
                  <span className="px-1 rounded" style={{ background: "#374151", fontSize: 10 }}>{st.stage}</span>
                </div>
              ))}
            </div>
          )}
        </div>
        {expanded ? <ChevronDown size={14} style={{ color: "#6b7280" }} /> : <ChevronRight size={14} style={{ color: "#6b7280" }} />}
      </div>

      {expanded && (
        <div className="mt-3 pt-3" style={{ borderTop: "1px solid rgba(55,65,81,0.5)" }}>
          <div className="flex items-center gap-0.5 mb-3">
            {STAGES.map((s, i) => {
              const stageIdx = STAGES.findIndex((x) => x.id === pbi.stage);
              const done = i < stageIdx || pbi.stage === "merge";
              const current = i === stageIdx && pbi.stage !== "merge";
              const Icon = s.icon;
              return (
                <div key={s.id} className="flex items-center gap-0.5">
                  <div className="flex items-center justify-center w-6 h-6 rounded-full" title={s.label} style={{
                    background: done ? "rgba(34,197,94,0.15)" : current ? "rgba(59,130,246,0.15)" : "#374151",
                  }}>
                    <Icon size={11} style={{ color: done ? "#22c55e" : current ? "#3b82f6" : "#6b7280" }} />
                  </div>
                  {i < STAGES.length - 1 && <div className="w-3 h-px" style={{ background: done ? "rgba(34,197,94,0.2)" : "rgba(55,65,81,0.4)" }} />}
                </div>
              );
            })}
          </div>

          <ReviewPanel reviewers={pbi.reviewers} />
          <EscalationChain escalation={pbi.escalation} escalationLog={pbi.escalationLog} />

          {pbi.log.length > 0 && (
            <div className="mt-3">
              <div className="text-xs font-medium mb-1.5" style={{ color: "#9ca3af" }}>Activity</div>
              <div className="space-y-1 max-h-44 overflow-y-auto pr-1" style={{ scrollbarWidth: "thin" }}>
                {pbi.log.map((entry, i) => (
                  <div key={i} className="flex items-start gap-2 text-xs">
                    <span className="font-mono w-10 shrink-0" style={{ color: "#6b7280" }}>{entry.time}</span>
                    <TierBadge tier={entry.agent} />
                    <span style={{ color: "#d1d5db" }}>{entry.action}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function RepoSection({ repo, expandedPbi, onTogglePbi }) {
  const [collapsed, setCollapsed] = useState(false);
  const queuedCount = repo.pbis.filter((p) => p.stage === "backlog").length;
  const doneCount = repo.pbis.filter((p) => p.stage === "merge").length;

  return (
    <div className="mb-3 rounded-xl overflow-hidden" style={{ background: "#1f2937", border: "1px solid #374151" }}>
      <div className="flex items-center justify-between p-3 cursor-pointer" onClick={() => setCollapsed(!collapsed)} style={{ borderBottom: collapsed ? "none" : "1px solid rgba(55,65,81,0.4)" }}>
        <div className="flex items-center gap-2">
          <FolderGit2 size={15} style={{ color: "#60a5fa" }} />
          <span className="text-sm font-semibold" style={{ color: "#f3f4f6" }}>{repo.name}</span>
        </div>
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-1.5 text-xs">
            <Pen size={11} style={{ color: repo.writer.status === "active" ? "#22c55e" : "#6b7280" }} />
            {repo.writer.status === "active" ? (
              <>
                <span className="w-1.5 h-1.5 rounded-full bg-green-500 animate-pulse" />
                <TierBadge tier="luna" />
                <span style={{ color: "#6b7280" }}>→ {repo.writer.currentPbi}</span>
              </>
            ) : (
              <span style={{ color: "#6b7280" }}>Idle</span>
            )}
          </div>
          <div className="flex items-center gap-2 text-xs" style={{ color: "#6b7280" }}>
            {queuedCount > 0 && <span>{queuedCount} queued</span>}
            {doneCount > 0 && <span className="flex items-center gap-0.5"><CheckCircle2 size={10} style={{ color: "#22c55e" }} />{doneCount}</span>}
          </div>
          {collapsed ? <ChevronRight size={14} style={{ color: "#6b7280" }} /> : <ChevronDown size={14} style={{ color: "#6b7280" }} />}
        </div>
      </div>

      {!collapsed && (
        <div className="p-2">
          {repo.pbis.map((pbi) => (
            <PbiCard key={pbi.id} pbi={pbi} expanded={expandedPbi === pbi.id} onToggle={() => onTogglePbi(pbi.id)} />
          ))}
        </div>
      )}
    </div>
  );
}

function ProjectStats({ project }) {
  const allPbis = project.repos.flatMap((r) => r.pbis);
  const active = allPbis.filter((p) => !["backlog", "merge"].includes(p.stage));
  const escalated = allPbis.filter((p) => p.escalation.consecutive > 0);
  const merged = allPbis.filter((p) => p.stage === "merge");
  const writers = project.repos.filter((r) => r.writer.status === "active");

  return (
    <div className="grid grid-cols-4 gap-2 mb-5">
      {[
        { label: "Writers", value: `${writers.length} / ${project.repos.length}`, color: "#22c55e", sub: "1 per repo" },
        { label: "In pipeline", value: active.length, color: "#60a5fa", sub: `${allPbis.length} total PBIs` },
        { label: "Escalated", value: escalated.length, color: escalated.length > 0 ? "#f59e0b" : "#6b7280", sub: escalated.length > 0 ? escalated.map((p) => p.id).join(", ") : "none" },
        { label: "Merged", value: merged.length, color: "#22c55e", sub: "today" },
      ].map((stat) => (
        <div key={stat.label} className="p-2.5 rounded-lg" style={{ background: "#1f2937", border: "1px solid #374151" }}>
          <div className="text-xs mb-1" style={{ color: "#9ca3af" }}>{stat.label}</div>
          <div className="text-xl font-bold" style={{ color: stat.color }}>{stat.value}</div>
          <div className="text-xs mt-0.5" style={{ color: "#6b7280" }}>{stat.sub}</div>
        </div>
      ))}
    </div>
  );
}

function EscalationLegend() {
  return (
    <div className="flex items-center gap-2 mb-5 p-2.5 rounded-lg flex-wrap" style={{ background: "#1f2937", border: "1px solid #374151" }}>
      <span className="text-xs" style={{ color: "#9ca3af" }}>Escalation:</span>
      {TIERS.map((t, i) => (
        <div key={t.id} className="flex items-center gap-1">
          <div className="w-2.5 h-2.5 rounded-full" style={{ background: t.color }} />
          <span className="text-xs font-medium" style={{ color: t.color }}>{t.label}</span>
          {i < TIERS.length - 1 && <ArrowRight size={9} style={{ color: "#4b5563", marginLeft: 1 }} />}
        </div>
      ))}
      <span className="text-xs ml-auto" style={{ color: "#6b7280" }}>Resets after each successful fix</span>
    </div>
  );
}

export default function SwarmDashboard() {
  const [project] = useState(SAMPLE_DATA);
  const [expandedPbi, setExpandedPbi] = useState("SW-31");

  const togglePbi = useCallback((id) => setExpandedPbi((prev) => (prev === id ? null : id)), []);

  return (
    <div className="min-h-screen p-4 sm:p-6" style={{ background: "#111827", color: "#e5e7eb", fontFamily: "system-ui, -apple-system, sans-serif" }}>
      <div className="max-w-3xl mx-auto">
        <div className="flex items-center justify-between mb-1">
          <div className="flex items-center gap-2.5">
            <h1 className="text-lg font-semibold" style={{ color: "#f3f4f6" }}>{project.name}</h1>
            <span className="text-xs px-2 py-0.5 rounded-full" style={{ background: "rgba(96,165,250,0.12)", color: "#60a5fa" }}>
              {project.repos.length} repos
            </span>
          </div>
          <div className="flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
            <span className="text-xs" style={{ color: "#22c55e" }}>Running</span>
          </div>
        </div>
        <div className="text-xs mb-5" style={{ color: "#6b7280" }}>
          1 writer per repo · N readers per PR · escalation resets on success
        </div>

        <EscalationLegend />
        <ProjectStats project={project} />

        {project.repos.map((repo) => (
          <RepoSection key={repo.name} repo={repo} expandedPbi={expandedPbi} onTogglePbi={togglePbi} />
        ))}
      </div>
    </div>
  );
}