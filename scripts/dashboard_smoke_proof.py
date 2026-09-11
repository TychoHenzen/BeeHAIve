"""Typed dashboard assertions and action proof."""

from __future__ import annotations

import io
import json
import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from beehaiive.provider import PROVIDER_REQUEST_TIMEOUT

from .dashboard_smoke_browser import (
    DevTools,
    action_log_snapshot,
    browser_fetches,
    clear_credentials,
    click_button,
    click_pbi_button,
    click_repository_button,
    click_selector,
    credential_surface_snapshot,
    dashboard_fetch_count,
    page_strings,
    set_input,
    status_snapshot,
    wait_for_action,
    wait_for_action_response,
    wait_for_status,
    wait_until,
)
from .dashboard_smoke_report import redact_project_id, redact_text, sanitize_fetches
from .dashboard_smoke_runtime import provider_identity_probe
from .dashboard_smoke_types import FIXTURE_API_KEY, SmokeFailure


def dashboard_snapshot(devtools: DevTools) -> dict[str, Any]:
    value = devtools.evaluate(
        """
(() => {
  const text = (node) => node?.textContent?.trim() || "";
  const summary = Object.fromEntries(
    [...document.querySelectorAll("#summary .card")]
      .map((card) => [
        text(card.querySelector(".label")),
        text(card.querySelector(".value")),
      ])
      .filter(([label]) => label),
  );
  return {
    project_name: text(document.querySelector(".project-meta h2")),
    updated: text(document.querySelector(".project-meta .muted"))
      || text(document.querySelector("#state-status")),
    status: text(document.querySelector("#state-status")),
    counts: summary,
    repositories: [...document.querySelectorAll(".repo h2")].map(text),
    pbis: [...document.querySelectorAll(".pbi-title")].map(text),
    pipeline_stages: [
      ...document.querySelectorAll(".pbi .progress li.current"),
    ].map(text),
    pbi_cards: [...document.querySelectorAll(".pbi")].map((card) => ({
      repository: card.dataset.repository || "",
      number: card.dataset.pbiNumber || "",
      text: text(card),
    })),
    metadata_sections: [...document.querySelectorAll(".pbi .details h3")].map(text),
    metadata_values: [...document.querySelectorAll(".pbi .details section")]
      .flatMap((section) => {
        const title = text(section.querySelector("h3"));
        return [...section.querySelectorAll("li, .muted")]
          .map(text)
          .filter(Boolean)
          .map((value) => `${title}: ${value}`);
      }),
    dashboard_text: text(document.querySelector("#dashboard")),
  };
})()
"""
    )
    if not isinstance(value, Mapping):
        return {
            "project_name": "",
            "updated": "",
            "status": "",
            "counts": {},
            "repositories": [],
            "pbis": [],
            "pipeline_stages": [],
            "pbi_cards": [],
            "metadata_sections": [],
            "metadata_values": [],
            "dashboard_text": "",
        }
    return dict(value)


def required_dashboard_fields(snapshot: Mapping[str, Any]) -> dict[str, bool]:
    counts = snapshot.get("counts")
    count_values = counts if isinstance(counts, Mapping) else {}
    required_counts = (
        "Projects",
        "Repositories",
        "PBIs",
        "Subtasks",
        "Writers",
        "Readers",
        "Active runs",
        "Failed runs",
        "Completed runs",
    )
    return {
        "project_name": bool(str(snapshot.get("project_name", "")).strip()),
        "counts": all(label in count_values for label in required_counts),
        "repositories": bool(snapshot.get("repositories")),
        "pbis": bool(snapshot.get("pbis")),
        "pipeline_stage": bool(snapshot.get("pipeline_stages")),
        "metadata": bool(snapshot.get("metadata_sections")),
        "updated_at": str(snapshot.get("updated", "")).startswith(
            ("Updated ", "Updated: ")
        ),
    }


def dashboard_payload(
    devtools: DevTools, *, archived: bool = False
) -> Mapping[str, Any]:
    dashboard_path = "/dashboard?archived=true" if archived else "/dashboard"
    for item in browser_fetches(devtools):
        url = str(item.get("url", ""))
        payload = item.get("payload")
        if (
            item.get("method") == "GET"
            and item.get("status") == 200
            and "/projects/" in url
            and url.endswith(dashboard_path)
            and isinstance(payload, Mapping)
        ):
            return payload
    raise SmokeFailure("The browser recorded no successful dashboard response")


def response_mappings(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def pull_request_line(item: Mapping[str, Any]) -> str:
    evidence: list[str] = []
    url = item.get("url")
    if isinstance(url, str) and url:
        evidence.append(url)
    source_branch = item.get("source_branch")
    if source_branch:
        branch_state = item.get("source_branch_state") or "unknown"
        evidence.append(f"branch {source_branch} ({branch_state})")
    suffix = f" ({', '.join(evidence)})" if evidence else ""
    if item.get("merged") is True:
        state = "merged"
    else:
        raw_state = item.get("state")
        state = raw_state.strip().lower() if isinstance(raw_state, str) else ""
        decision = item.get("review_decision")
        decision_text = decision if isinstance(decision, str) else ""
        if state == "open" and not decision_text:
            state = "open, review pending"
        elif state and decision_text:
            state = f"{state}, {decision_text}"
        else:
            state = state or decision_text or "review pending"
    return f"#{item.get('number')}: {state}{suffix}"


def response_metadata_lines(payload: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    for repository in response_mappings(payload.get("repositories")):
        for pbi in response_mappings(repository.get("pbis")):
            for title, key, formatter in (
                (
                    "Subtasks",
                    "subtasks",
                    lambda item: (
                        f"{item.get('id') or 'subtask'}: {item.get('title') or ''}"
                    ),
                ),
                (
                    "Pull requests",
                    "pull_requests",
                    pull_request_line,
                ),
                (
                    "Readers",
                    "readers",
                    lambda item: (
                        f"{
                            ('PR #' + str(item['pull_request']) + ' ')
                            if item.get('pull_request')
                            else ''
                        }"
                        f"{item.get('id') or item.get('name') or 'reader'}: "
                        f"{item.get('status') or 'pending'}"
                    ),
                ),
            ):
                values = response_mappings(pbi.get(key))
                if values:
                    lines.extend(f"{title}: {formatter(item)}" for item in values)
                else:
                    lines.append(f"{title}: None")

            reviewers = pbi.get("reviewers")
            reviewer_values = (
                reviewers.items() if isinstance(reviewers, Mapping) else ()
            )
            reviewer_lines = [
                f"{name}: {result.get('status') or 'pending'}"
                + (f" Â· {result['comment']}" if result.get("comment") else "")
                for name, result in reviewer_values
                if isinstance(result, Mapping)
            ]
            lines.extend(f"Reviewer results: {line}" for line in reviewer_lines)
            if not reviewer_lines:
                lines.append("Reviewer results: None")

            escalation = pbi.get("escalation")
            current = escalation if isinstance(escalation, Mapping) else {}
            tier = current.get("current_tier", current.get("current"))
            consecutive = current.get("consecutive", 0)
            lines.append(f"Escalation: Tier {tier}; {consecutive} consecutive failures")
            for entry in response_values(pbi.get("escalation_log")):
                lines.append(
                    "Escalation: "
                    + json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
                )

            activity = response_values(pbi.get("activity"))
            if activity:
                for raw_entry in reversed(activity):
                    entry = raw_entry if isinstance(raw_entry, Mapping) else {}
                    transition = (
                        f" {entry['from_stage']} -> {entry['to_stage']}"
                        if entry.get("from_stage") and entry.get("to_stage")
                        else ""
                    )
                    timestamp = entry.get("created_at", entry.get("time", ""))
                    description = entry.get("action", entry.get("type", "event"))
                    lines.append(
                        f"Activity: {timestamp} {description}{transition}".strip()
                    )
            else:
                lines.append("Activity: None")
    return lines


def live_terminal_pbi_proof(
    default_payload: Mapping[str, Any],
    default_snapshot: Mapping[str, Any],
    archived_payload: Mapping[str, Any],
    archived_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    target_numbers = {7, 8, 9}
    target_pbis: dict[int, Mapping[str, Any]] = {}
    for repository in response_mappings(archived_payload.get("repositories")):
        if repository.get("name") != "TychoHenzen/BeeHAIve":
            continue
        for pbi in response_mappings(repository.get("pbis")):
            number = pbi.get("number")
            if isinstance(number, int) and number in target_numbers:
                target_pbis[number] = pbi
    if set(target_pbis) != target_numbers:
        raise SmokeFailure(
            "Archived live proof did not return BeeHAIve issues #7, #8, and #9"
        )

    default_pbi_numbers = {
        pbi.get("number")
        for repository in response_mappings(default_payload.get("repositories"))
        if repository.get("name") == "TychoHenzen/BeeHAIve"
        for pbi in response_mappings(repository.get("pbis"))
    }
    if default_pbi_numbers.intersection(target_numbers):
        raise SmokeFailure("Default live proof still returned archived target PBIs")
    default_cards = response_mappings(default_snapshot.get("pbi_cards"))
    if any(
        card.get("repository") == "TychoHenzen/BeeHAIve"
        and str(card.get("number")) in {str(number) for number in target_numbers}
        for card in default_cards
    ):
        raise SmokeFailure("Default live proof still rendered archived target PBIs")
    cards = [
        card
        for card in response_mappings(archived_snapshot.get("pbi_cards"))
        if card.get("repository") == "TychoHenzen/BeeHAIve"
    ]
    evidence: dict[str, Any] = {}
    for number in sorted(target_numbers):
        pbi = target_pbis[number]
        if pbi.get("planning_status") != "Done":
            raise SmokeFailure(f"Live issue #{number} did not show Project status Done")
        merged_pull_requests = [
            pr
            for pr in response_mappings(pbi.get("pull_requests"))
            if pr.get("merged") is True
        ]
        if not merged_pull_requests:
            raise SmokeFailure(
                f"Live issue #{number} did not show a merged pull request"
            )
        progress = response_mappings(pbi.get("stage_progress"))
        if not any(
            item.get("id") == "merge" and item.get("status") == "current"
            for item in progress
        ):
            raise SmokeFailure(f"Live issue #{number} did not show terminal progress")
        card = next(
            (item for item in cards if str(item.get("number")) == str(number)),
            None,
        )
        card_text = str(card.get("text", "")) if card else ""
        if "Project status: Done" not in card_text:
            raise SmokeFailure(f"Live issue #{number} card omitted Project status Done")
        if "review pending" in card_text.lower():
            raise SmokeFailure(f"Live issue #{number} card still showed review pending")
        evidence[str(number)] = {
            "project_status": pbi.get("planning_status"),
            "stage_label": pbi.get("stage_label"),
            "merged_pull_requests": [
                {"number": pr.get("number"), "merged": pr.get("merged")}
                for pr in merged_pull_requests
            ],
            "card_project_status": "Done",
            "card_review_pending": False,
            "default_projection": "omitted",
            "archived_projection": "included",
        }
    return evidence


def response_values(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    return value


def response_backed_dashboard_fields(
    payload: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    project_id: str,
) -> dict[str, bool]:
    counts = payload.get("counts")
    rendered_counts = snapshot.get("counts")
    expected_counts = {
        "Projects": counts.get("projects") if isinstance(counts, Mapping) else None,
        "Repositories": (
            f"{counts.get('active_repositories', 0)} / {counts.get('repositories', 0)}"
            if isinstance(counts, Mapping)
            else None
        ),
        "PBIs": counts.get("pbis") if isinstance(counts, Mapping) else None,
        "Subtasks": counts.get("subtasks") if isinstance(counts, Mapping) else None,
        "Writers": counts.get("writers") if isinstance(counts, Mapping) else None,
        "Readers": counts.get("readers") if isinstance(counts, Mapping) else None,
        "Active runs": (
            counts.get("active_runs") if isinstance(counts, Mapping) else None
        ),
        "Failed runs": (
            counts.get("failed_runs") if isinstance(counts, Mapping) else None
        ),
        "Completed runs": (
            counts.get("completed_runs") if isinstance(counts, Mapping) else None
        ),
    }
    count_values = rendered_counts if isinstance(rendered_counts, Mapping) else {}
    count_match = all(
        label in count_values and str(count_values[label]) == str(expected)
        for label, expected in expected_counts.items()
    )
    expected_repositories: list[str] = []
    expected_pbis: list[str] = []
    expected_stages: list[str] = []
    for repository in response_mappings(payload.get("repositories")):
        name = repository.get("name")
        if isinstance(name, str):
            state = "active" if repository.get("active") is True else "inactive"
            expected_repositories.append(f"{name} {state}")
        for pbi in response_mappings(repository.get("pbis")):
            title = pbi.get("title")
            if isinstance(title, str):
                expected_pbis.append(title)
            for stage in response_mappings(pbi.get("stage_progress")):
                if stage.get("status") == "current" and isinstance(
                    stage.get("label"), str
                ):
                    expected_stages.append(str(stage["label"]))
    rendered_repositories = snapshot.get("repositories")
    rendered_pbis = snapshot.get("pbis")
    rendered_stages = snapshot.get("pipeline_stages")
    metadata_values = snapshot.get("metadata_values")
    rendered_metadata = (
        [str(value) for value in metadata_values]
        if isinstance(metadata_values, list)
        else []
    )
    expected_metadata = response_metadata_lines(payload)
    updated = str(snapshot.get("updated", ""))
    status = str(snapshot.get("status", ""))
    updated_at = payload.get("updated_at")
    return {
        "project_id": payload.get("project_id") == project_id,
        "project_name": payload.get("name") == snapshot.get("project_name"),
        "counts": count_match,
        "repositories": rendered_repositories == expected_repositories,
        "pbis": rendered_pbis == expected_pbis,
        "pipeline_stage": rendered_stages == expected_stages,
        "metadata": rendered_metadata == expected_metadata,
        "updated_at": (
            isinstance(updated_at, str)
            and updated == f"Updated: {updated_at}"
            and status == f"Updated {updated_at}."
        ),
    }


def record_action(
    devtools: DevTools,
    outcomes: dict[str, dict[str, Any]],
    name: str,
    expected: str | None = None,
    before_action_count: int = 0,
    timeout: float = 15.0,
    *,
    action_name: str | None = None,
) -> dict[str, Any]:
    action = action_name or name.split("_", 1)[0]
    pending = status_snapshot(devtools)
    if not pending["message"].startswith(f"{action} pending"):
        raise SmokeFailure(
            f"{name} did not expose a pending state: {pending['message']!r}"
        )
    result = wait_for_action(devtools, action, timeout=timeout)
    response = wait_for_action_response(devtools, action)
    payload = response.get("payload")
    if not isinstance(payload, Mapping):
        raise SmokeFailure(f"The {name} action response had no JSON payload")
    action_result = payload.get("action")
    if not isinstance(action_result, Mapping):
        raise SmokeFailure(f"The {name} action response omitted action state")
    expected_status = (
        "failed" if expected and expected.startswith("failed") else "succeeded"
    )
    if action_result.get("status") != expected_status:
        raise SmokeFailure(
            f"{name} returned action status {action_result.get('status')!r}, "
            f"expected {expected_status!r}"
        )
    if not isinstance(payload.get("state"), Mapping):
        raise SmokeFailure(f"The {name} action response omitted dashboard state")
    if expected_status == "succeeded" and not isinstance(
        payload.get("result"), Mapping
    ):
        raise SmokeFailure(f"The {name} action response omitted its result")
    if expected_status == "failed" and payload.get("result") is not None:
        raise SmokeFailure(
            f"The {name} failure response unexpectedly returned a result"
        )
    visible_actions = action_log_snapshot(devtools)
    if (
        not visible_actions["visible"]
        or len(visible_actions["rows"]) <= before_action_count
    ):
        raise SmokeFailure(f"The {name} action was not rendered in the action log")
    visible_dashboard = dashboard_snapshot(devtools)
    if not visible_dashboard.get("dashboard_text"):
        raise SmokeFailure(f"The {name} action left no visible dashboard state")
    outcomes[name] = {
        "pending_message": pending["message"],
        "pending_class_name": pending["class_name"],
        "result_message": result["message"],
        "result_class_name": result["class_name"],
        "http_status": response.get("status"),
        "action_status": action_result.get("status"),
        "returned_state": True,
        "result_present": payload.get("result") is not None,
        "visible_action_log": {
            "visible": visible_actions["visible"],
            "count": len(visible_actions["rows"]),
        },
        "visible_dashboard": {
            "project_name": visible_dashboard.get("project_name"),
            "updated": visible_dashboard.get("updated"),
            "repository_count": len(visible_dashboard.get("repositories", [])),
            "pbi_count": len(visible_dashboard.get("pbis", [])),
            "pipeline_stage_count": len(visible_dashboard.get("pipeline_stages", [])),
        },
    }
    if expected is not None and not result["message"].startswith(
        f"{action} {expected}"
    ):
        raise SmokeFailure(
            f"{name} returned {result['message']!r}, expected {expected!r}"
        )
    return response


def run_fixture_actions(devtools: DevTools) -> dict[str, dict[str, Any]]:
    outcomes: dict[str, dict[str, Any]] = {}
    set_input(devtools, "#api-key", FIXTURE_API_KEY)
    before = len(action_log_snapshot(devtools)["rows"])
    if not click_selector(devtools, "#start"):
        raise SmokeFailure("The dashboard sync button was not rendered")
    record_action(devtools, outcomes, "start_sync", "succeeded.", before)

    before = len(action_log_snapshot(devtools)["rows"])
    if not click_repository_button(devtools, "owner/api", "Start writer"):
        raise SmokeFailure("The dashboard start-writer action was not rendered")
    record_action(devtools, outcomes, "start_writer", "succeeded.", before)

    before = len(action_log_snapshot(devtools)["rows"])
    if not click_button(devtools, "Record approval"):
        raise SmokeFailure("The dashboard approval action was not rendered")
    record_action(devtools, outcomes, "approve", "succeeded.", before)

    devtools.evaluate("window.prompt = () => 'Use the deterministic fixture';")
    before = len(action_log_snapshot(devtools)["rows"])
    if not click_button(devtools, "Request clarification"):
        raise SmokeFailure("The dashboard clarification action was not rendered")
    record_action(devtools, outcomes, "clarify", "succeeded.", before)

    before = len(action_log_snapshot(devtools)["rows"])
    if not click_button(devtools, "Stop"):
        raise SmokeFailure("The dashboard stop action was not rendered")
    record_action(devtools, outcomes, "stop", "succeeded.", before)

    before = len(action_log_snapshot(devtools)["rows"])
    if not click_repository_button(devtools, "owner/empty", "Start writer"):
        raise SmokeFailure("The dashboard failure fixture was not rendered")
    record_action(devtools, outcomes, "start_empty", "failed:", before)
    return outcomes


def action_run_target(
    response: Mapping[str, Any], name: str
) -> tuple[str, int, str, int]:
    payload = response.get("payload")
    result = payload.get("result") if isinstance(payload, Mapping) else None
    run = result.get("run") if isinstance(result, Mapping) else None
    repository = run.get("repository") if isinstance(run, Mapping) else None
    pbi_number = run.get("pbi_number") if isinstance(run, Mapping) else None
    run_id = run.get("run_id") if isinstance(run, Mapping) else None
    attempt = run.get("attempt") if isinstance(run, Mapping) else None
    if (
        not isinstance(repository, str)
        or not isinstance(pbi_number, int)
        or not isinstance(run_id, str)
        or not isinstance(attempt, int)
    ):
        raise SmokeFailure(f"The {name} response omitted its run identity")
    return repository, pbi_number, run_id, attempt


def wait_for_demo_outcome(
    devtools: DevTools,
    repository: str,
    pbi_number: int,
    run_id: str,
    attempt: int,
    timeout: float,
) -> dict[str, str]:
    value = wait_until(
        devtools,
        f"""
(() => {{
  const pbi = [...document.querySelectorAll('.pbi')].find((node) =>
    node.dataset.repository === {json.dumps(repository)}
    && node.dataset.pbiNumber === {json.dumps(str(pbi_number))}
    && node.dataset.runId === {json.dumps(run_id)}
    && node.dataset.attempt === {json.dumps(str(attempt))}
  );
  const text = pbi?.textContent?.trim() || '';
  if (text.includes('Result:')) return {{ status: 'completed', text }};
  if (text.includes('Failure:')) return {{ status: 'failed', text }};
  return null;
}})()
""",
        "bounded demo result",
        timeout=timeout,
    )
    if not isinstance(value, Mapping):
        raise SmokeFailure("The live dashboard returned no bounded demo outcome")
    status = value.get("status")
    text = value.get("text")
    if not isinstance(status, str) or not isinstance(text, str):
        raise SmokeFailure(
            "The live dashboard returned an invalid bounded demo outcome"
        )
    return {"status": status, "text": text}


def run_live_actions(
    devtools: DevTools, api_key: str, timeout: float = 60.0
) -> dict[str, dict[str, Any]]:
    outcomes: dict[str, dict[str, Any]] = {}
    set_input(devtools, "#api-key", api_key)
    before = len(action_log_snapshot(devtools)["rows"])
    if not click_selector(devtools, "#start"):
        raise SmokeFailure("The dashboard sync button was not rendered")
    record_action(
        devtools, outcomes, "start_sync", before_action_count=before, timeout=timeout
    )

    before = len(action_log_snapshot(devtools)["rows"])
    repository_hint = os.environ.get("BEEHAIIVE_AGENT_REPOSITORY_NAME", "").strip()
    if not repository_hint:
        raise SmokeFailure("Live mutation proof needs BEEHAIIVE_AGENT_REPOSITORY_NAME")
    if not click_repository_button(devtools, repository_hint, "Start writer"):
        raise SmokeFailure("The live dashboard did not render the start-writer action")
    start_response = record_action(
        devtools, outcomes, "start_writer", before_action_count=before, timeout=timeout
    )
    repository, pbi_number, run_id, _ = action_run_target(
        start_response, "start_writer"
    )

    before = len(action_log_snapshot(devtools)["rows"])
    if not click_pbi_button(
        devtools, repository, pbi_number, run_id, "Record approval"
    ):
        raise SmokeFailure(
            "The live approval action was not rendered for the targeted run"
        )
    record_action(
        devtools, outcomes, "approve", before_action_count=before, timeout=timeout
    )

    devtools.evaluate("window.prompt = () => 'Use the live bounded demo';")
    before = len(action_log_snapshot(devtools)["rows"])
    if not click_pbi_button(
        devtools, repository, pbi_number, run_id, "Request clarification"
    ):
        raise SmokeFailure(
            "The live clarification action was not rendered for the targeted run"
        )
    record_action(
        devtools, outcomes, "clarify", before_action_count=before, timeout=timeout
    )

    before = len(action_log_snapshot(devtools)["rows"])
    if not click_pbi_button(devtools, repository, pbi_number, run_id, "Stop"):
        raise SmokeFailure("The live stop action was not rendered for the targeted run")
    record_action(
        devtools, outcomes, "stop", before_action_count=before, timeout=timeout
    )

    before = len(action_log_snapshot(devtools)["rows"])
    if not click_repository_button(devtools, repository_hint, "Start writer"):
        raise SmokeFailure("The live completion run was not rendered")
    completion_response = record_action(
        devtools,
        outcomes,
        "completion_writer",
        before_action_count=before,
        timeout=timeout,
        action_name="start",
    )
    completion_repository, completion_pbi, completion_run, completion_attempt = (
        action_run_target(completion_response, "completion_writer")
    )
    demo_outcome = wait_for_demo_outcome(
        devtools,
        completion_repository,
        completion_pbi,
        completion_run,
        completion_attempt,
        timeout,
    )
    if demo_outcome["status"] != "completed":
        raise SmokeFailure("The live bounded demo reported a failure")
    outcomes["demo_result"] = {
        "status": demo_outcome["status"],
        "visible_result": "Result:" in demo_outcome["text"],
        "targeted_run": True,
    }
    return outcomes


def configured_live_timeout(configured: float | None) -> float:
    if configured is not None:
        if configured <= 0:
            raise SmokeFailure("Live smoke timeout must be greater than zero")
        return configured
    return PROVIDER_REQUEST_TIMEOUT * 2 + 5.0


def run_browser_smoke(
    devtools: DevTools,
    base_url: str,
    project_id: str,
    mode: str,
    allow_mutations: bool,
    provider: Any,
    secrets_to_redact: tuple[str, ...],
    server_output: io.StringIO,
    live_timeout: float | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {"passed": False, "actions": {}}
    report["provider_identity"] = provider_identity_probe(project_id)
    refresh_timeout = (
        15.0 if mode == "fixture" else configured_live_timeout(live_timeout)
    )
    initial_status = wait_for_status(
        devtools,
        "Updated ",
        "initial dashboard refresh",
        timeout=refresh_timeout,
    )
    visible_text, page_html = page_strings(devtools)
    initial_view = dashboard_snapshot(devtools)
    initial_payload = dashboard_payload(devtools)
    required_fields = required_dashboard_fields(initial_view)
    response_fields = response_backed_dashboard_fields(
        initial_payload, initial_view, project_id
    )
    missing_fields = [name for name, present in required_fields.items() if not present]
    missing_response_fields = [
        name for name, present in response_fields.items() if not present
    ]
    if missing_fields:
        raise SmokeFailure(
            "The dashboard omitted required rendered fields: "
            + ", ".join(missing_fields)
        )
    if missing_response_fields:
        raise SmokeFailure(
            "The dashboard did not match response fields: "
            + ", ".join(missing_response_fields)
        )
    live_terminal_evidence = None
    if mode == "live" and project_id == "TychoHenzen:2":
        if not devtools.evaluate("document.querySelector('#archived-view')"):
            raise SmokeFailure("The live dashboard omitted the archived view control")
        devtools.evaluate("document.querySelector('#archived-view').click()")
        wait_for_status(
            devtools,
            "Updated ",
            "archived dashboard refresh",
            timeout=refresh_timeout,
        )
        archived_view = dashboard_snapshot(devtools)
        archived_payload = dashboard_payload(devtools, archived=True)
        live_terminal_evidence = live_terminal_pbi_proof(
            initial_payload, initial_view, archived_payload, archived_view
        )
        devtools.evaluate("document.querySelector('#archived-view').click()")
        wait_for_status(
            devtools,
            "Updated ",
            "default dashboard refresh after archive proof",
            timeout=refresh_timeout,
        )
    api_key_value = devtools.evaluate("document.querySelector('#api-key')?.value || ''")
    if api_key_value:
        raise SmokeFailure("The initial read-only dashboard requested an API key")
    for expected in (
        "Projects",
        "Repositories",
        "PBIs",
        "Subtasks",
        "Writers",
        "Readers",
        "Completed runs",
    ):
        if expected not in visible_text:
            raise SmokeFailure(f"The dashboard summary omitted {expected}")
    if mode == "fixture":
        for expected in (
            "Fixture Project",
            "Dashboard proof PBI",
            "Check API",
            "Tier terra",
        ):
            if expected not in page_html:
                raise SmokeFailure(f"The fixture dashboard omitted {expected}")
    report["initial_refresh"] = {
        "ui": initial_status,
        "required_fields": required_fields,
        "response_backed_fields": response_fields,
        "project_name": initial_view["project_name"],
        "updated": initial_view["updated"],
        "counts": initial_view["counts"],
        "repositories": initial_view["repositories"][:5],
        "pbis": initial_view["pbis"][:5],
        "pipeline_stages": initial_view["pipeline_stages"][:5],
        "metadata_sections": initial_view["metadata_sections"][:10],
    }
    if live_terminal_evidence is not None:
        report["live_terminal_project_state"] = live_terminal_evidence

    polling_baseline = dashboard_fetch_count(devtools)
    polling_delay = wait_until(
        devtools,
        f"""
(() => {{
  const records = (window.__beehaiiveSmokeFetches || [])
    .filter((record) => record.method === "GET"
      && record.url.includes("/projects/")
      && record.url.endsWith("/dashboard")
      && record.status === 200).length;
  const successful = (window.__beehaiiveSmokeFetches || [])
    .filter((record) => record.method === "GET"
      && record.url.includes("/projects/")
      && record.url.endsWith("/dashboard")
      && record.status === 200);
  const candidate = successful.at(-1);
  const pageStartedAt = window.__beehaiiveSmokePageStartedAt || 0;
  const delay = candidate ? candidate.started_at - pageStartedAt : 0;
  return records > {polling_baseline} && delay >= 4500 ? delay : 0;
}})()
""",
        "dashboard polling refresh",
        timeout=8.0,
    )
    if not isinstance(polling_delay, (int, float)):
        raise SmokeFailure("The dashboard polling timestamp was not recorded")
    report["polling"] = {
        "successful_dashboard_gets": dashboard_fetch_count(devtools),
        "new_successful_get_after_initial_refresh": True,
        "first_poll_delay_ms": round(float(polling_delay)),
    }
    discoveries_before_rejection = provider.discoveries

    invalid_project = "not-allowed:0"
    set_input(devtools, "#project-id", invalid_project, change=True)
    rejected_status = wait_for_status(
        devtools,
        "Project is not authorized",
        "unallowlisted project rejection",
        timeout=refresh_timeout,
    )
    invalid_project_paths = (
        f"/projects/{invalid_project}/dashboard",
        f"/projects/{quote(invalid_project, safe='')}/dashboard",
    )
    rejection_response = next(
        (
            item
            for item in browser_fetches(devtools)
            if item.get("method") == "GET"
            and item.get("status") == 403
            and any(path in str(item.get("url", "")) for path in invalid_project_paths)
        ),
        None,
    )
    if rejection_response is None:
        raise SmokeFailure(
            "The unallowlisted dashboard request did not return HTTP 403"
        )
    if provider.discoveries != discoveries_before_rejection:
        raise SmokeFailure("The unallowlisted request reached the provider")
    report["unallowlisted_project"] = {
        "project_id": redact_project_id(invalid_project),
        "ui": rejected_status,
        "provider_access": "blocked before provider discovery",
    }

    set_input(devtools, "#project-id", project_id, change=True)
    wait_for_status(
        devtools,
        "Updated ",
        "dashboard refresh after rejection",
        timeout=refresh_timeout,
    )
    if mode == "fixture":
        report["actions"] = run_fixture_actions(devtools)
    elif allow_mutations:
        if not os.environ.get("BEEHAIIVE_API_KEY"):
            raise SmokeFailure("Live mutation proof needs BEEHAIIVE_API_KEY")
        report["actions"] = run_live_actions(
            devtools,
            os.environ["BEEHAIIVE_API_KEY"],
            timeout=refresh_timeout,
        )
    else:
        report["actions"] = {"skipped": "pass --allow-mutations for live action proof"}

    expected_api_key = (
        FIXTURE_API_KEY
        if mode == "fixture"
        else os.environ.get("BEEHAIIVE_API_KEY", "")
        if allow_mutations
        else ""
    )
    credential_surface = credential_surface_snapshot(devtools, expected_api_key)
    if expected_api_key and not (
        credential_surface["configured_key_in_password_input"]
        and credential_surface["configured_key_in_local_storage"]
    ):
        raise SmokeFailure(
            "The configured API key was not confined to the password input and storage"
        )
    if not expected_api_key and (
        credential_surface["input_value_present"]
        or credential_surface["storage_value_present"]
    ):
        raise SmokeFailure("An unexpected API key remained in the browser")
    if any(
        credential_surface[key]
        for key in (
            "input_value_leaked_to_visible_page",
            "input_value_leaked_to_markup",
            "storage_value_leaked_to_visible_page",
            "storage_value_leaked_to_markup",
        )
    ):
        raise SmokeFailure("An API key appeared in rendered dashboard content")
    visible_text, page_html = page_strings(devtools)
    sensitive_values = tuple(value for value in secrets_to_redact if value)
    if any(secret in visible_text for secret in sensitive_values):
        raise SmokeFailure("A credential appeared in the dashboard page")
    if any(secret in page_html for secret in sensitive_values):
        raise SmokeFailure("A credential appeared in the dashboard page")
    service_output = server_output.getvalue()
    redacted_service_output = redact_text(service_output, sensitive_values, project_id)
    if any(secret in service_output for secret in sensitive_values):
        raise SmokeFailure("The isolated service output contained a credential")
    storage_cleared = clear_credentials(devtools)
    if not storage_cleared:
        raise SmokeFailure("The browser did not clear the API key from local storage")
    report["credential_safety"] = {
        "api_key_or_provider_token_in_visible_page": False,
        "api_key_or_provider_token_in_page_markup": False,
        "service_output": {
            "captured": True,
            "redacted": True,
            "lines": len(redacted_service_output.splitlines()),
            "contains_secret": False,
        },
        "browser_storage_cleared": storage_cleared,
        "credential_surface": credential_surface,
        "report_values_redacted": True,
    }
    report["http"] = sanitize_fetches(browser_fetches(devtools), project_id)
    report["service_url"] = base_url
    report["project_id"] = redact_project_id(project_id)
    report["passed"] = True
    return report
