import assert from "node:assert/strict";
import { test } from "node:test";

import {
  backgroundText,
  folderName,
  formatAgo,
  formatClock,
  formatDuration,
  formatTokens,
  plural,
  shortStage,
  splitIssueRefs,
  toolSummary,
} from "../beehaiive/web/format.js";

test("formatDuration picks the two largest units", () => {
  assert.equal(formatDuration(null), "");
  assert.equal(formatDuration(Number.NaN), "");
  assert.equal(formatDuration(-3), "0s");
  assert.equal(formatDuration(42.4), "42s");
  assert.equal(formatDuration(65), "1m 05s");
  assert.equal(formatDuration(3 * 3600 + 7 * 60), "3h 07m");
  assert.equal(formatDuration(50 * 3600), "2d 2h");
});

test("formatAgo measures from the given now", () => {
  const now = Date.parse("2026-11-05T12:10:00Z");
  assert.equal(formatAgo("2026-11-05T12:09:58Z", now), "just now");
  assert.equal(formatAgo("2026-11-05T12:00:00Z", now), "10m 00s ago");
  assert.equal(formatAgo(null, now), "");
  assert.equal(formatAgo("garbage", now), "");
});

test("formatClock ignores missing or bad times", () => {
  assert.equal(formatClock(null), "");
  assert.equal(formatClock("garbage"), "");
  assert.match(formatClock("2026-11-05T12:00:00Z"), /\d{2}.\d{2}.\d{2}/);
});

test("formatTokens shortens large counts", () => {
  assert.equal(formatTokens(0), "0");
  assert.equal(formatTokens(999), "999");
  assert.equal(formatTokens(1234), "1.2k");
  assert.equal(formatTokens(45_600), "46k");
  assert.equal(formatTokens(2_500_000), "2.5M");
});

test("shortStage shortens goal-sdlc skills and keeps the rest", () => {
  assert.equal(shortStage(null), "");
  assert.equal(shortStage("submit-draft-pr"), "draft PR");
  assert.equal(shortStage("fix-pr-review round 2"), "fix review round 2");
  assert.equal(shortStage("review-pr"), "review");
  assert.equal(shortStage("next-ticket"), "next-ticket");
  assert.equal(shortStage("review-prx"), "review-prx");
});

test("toolSummary and plural describe counts in words", () => {
  assert.equal(plural(1, "run"), "1 run");
  assert.equal(plural(2, "run"), "2 runs");
  assert.equal(toolSummary(0, []), "no tool calls");
  assert.equal(toolSummary(1, null), "1 tool call");
  assert.equal(toolSummary(5, [["Edit", 3], ["Bash", 2]]), "5 tool calls: Edit 3, Bash 2");
});

test("backgroundText lists only the counts that are not zero", () => {
  assert.equal(backgroundText(null), "");
  assert.equal(backgroundText({ hook_blocks: 0, tool_errors: 0, api_errors: 0, unreadable_records: 0 }), "");
  assert.equal(
    backgroundText({ hook_blocks: 1, tool_errors: 3, api_errors: 0, unreadable_records: 2 }),
    "1 hook block, 3 recovered tool errors, 2 unreadable records",
  );
});

test("splitIssueRefs links #N to the repository", () => {
  const repo = "https://github.com/o/r";
  assert.deepEqual(splitIssueRefs("", repo), []);
  assert.deepEqual(splitIssueRefs("fix #12 now", null), [{ text: "fix #12 now" }]);
  assert.deepEqual(splitIssueRefs("fix #12 and (#3)", repo), [
    { text: "fix " },
    { text: "#12", href: `${repo}/issues/12` },
    { text: " and (" },
    { text: "#3", href: `${repo}/issues/3` },
    { text: ")" },
  ]);
  assert.deepEqual(splitIssueRefs("#7", repo), [{ text: "#7", href: `${repo}/issues/7` }]);
  assert.deepEqual(splitIssueRefs("color#fff", repo), [{ text: "color#fff" }]);
});

test("folderName returns the last path part", () => {
  assert.equal(folderName("C:\\Users\\me\\repo"), "repo");
  assert.equal(folderName("/home/me/repo/"), "repo");
  assert.equal(folderName(null), "");
});
