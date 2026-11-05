import assert from "node:assert/strict";
import { test } from "node:test";

import {
  extraArgs,
  folderName,
  formatAgo,
  formatClock,
  formatDuration,
  formatTokens,
  splitIssueRefs,
  stageText,
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

test("stageText adds the issue number when there is one", () => {
  assert.equal(stageText(null), "");
  assert.equal(stageText({ stage: "next-ticket", issue: 405 }), "next-ticket #405");
  assert.equal(stageText({ stage: "goal-sdlc", issue: null }), "goal-sdlc");
});

test("extraArgs drops the issue number that stageText shows", () => {
  assert.equal(extraArgs(null), "");
  assert.equal(extraArgs({ args: "", issue: null }), "");
  assert.equal(extraArgs({ args: "--resume", issue: null }), "--resume");
  assert.equal(extraArgs({ args: "410", issue: 410 }), "");
  assert.equal(extraArgs({ args: "#405 (resume at checkpoint 5)", issue: 405 }), "(resume at checkpoint 5)");
  assert.equal(extraArgs({ args: "4105 x", issue: 410 }), "4105 x");
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
