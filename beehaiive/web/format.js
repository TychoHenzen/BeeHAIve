// Pure display helpers, kept free of the DOM so node --test can cover them.

export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "";
  const total = Math.max(0, Math.round(seconds));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes}m ${String(total % 60).padStart(2, "0")}s`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h ${String(minutes % 60).padStart(2, "0")}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

export function formatAgo(iso, nowMs) {
  if (!iso) return "";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const seconds = (nowMs - then) / 1000;
  if (seconds < 5) return "just now";
  return `${formatDuration(seconds)} ago`;
}

export function formatClock(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function formatTokens(count) {
  if (!count) return "0";
  if (count < 1000) return String(count);
  if (count < 1_000_000) return `${(count / 1000).toFixed(count < 10_000 ? 1 : 0)}k`;
  return `${(count / 1_000_000).toFixed(1)}M`;
}

export function stageText(stage) {
  if (!stage) return "";
  return stage.issue ? `${stage.stage} #${stage.issue}` : stage.stage;
}

// Skill arguments without the issue number that stageText already shows.
export function extraArgs(label) {
  if (!label?.args) return "";
  if (!label.issue) return label.args;
  return label.args.replace(new RegExp(`^#?${label.issue}\\b\\s*`), "");
}

// Split text into plain and linked pieces so "#123" can point at the repository.
export function splitIssueRefs(text, repositoryUrl) {
  if (!text) return [];
  if (!repositoryUrl) return [{ text }];
  const pieces = [];
  const pattern = /(^|[\s(])#(\d{1,7})\b/g;
  let last = 0;
  let match;
  while ((match = pattern.exec(text)) !== null) {
    const start = match.index + match[1].length;
    if (start > last) pieces.push({ text: text.slice(last, start) });
    pieces.push({ text: `#${match[2]}`, href: `${repositoryUrl}/issues/${match[2]}` });
    last = start + match[2].length + 1;
  }
  if (last < text.length) pieces.push({ text: text.slice(last) });
  return pieces;
}

export function folderName(path) {
  if (!path) return "";
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? path;
}
