from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from beehaiive.models import PullRequestSnapshot
from beehaiive.review import (
    FindingPublicationState,
    PullRequestTarget,
    ReaderStatus,
    ReviewConcern,
    ReviewError,
    ReviewRepairStatus,
    ReviewService,
    ReviewStore,
)
from beehaiive.review_repair import ReviewRepairService
from beehaiive.routing import AttemptOutcome, ModelExecution, ModelRouter, RoutingStore
from beehaiive.storage import OrchestratorStore
from beehaiive.workflow import (
    CheckResult,
    Constitution,
    WorkflowService,
    WorkflowStore,
)
from main import create_app

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def _git(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, Path, str, str]:
    remote = tmp_path / "owner" / "repo.git"
    remote.parent.mkdir()
    _git(tmp_path, "init", "--bare", "--initial-branch=master", str(remote))
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "master")
    _git(repository, "config", "user.email", "tests@example.test")
    _git(repository, "config", "user.name", "Review Repair Tests")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "base")
    base_head = _git(repository, "rev-parse", "HEAD")
    _git(repository, "switch", "-c", "feature")
    (repository / "README.md").write_text("feature\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "feature")
    source_head = _git(repository, "rev-parse", "HEAD")
    _git(repository, "remote", "add", "origin", str(remote))
    _git(repository, "push", "origin", "master", "feature")
    return repository, remote, base_head, source_head


class PassingCheck:
    name = "fixture"

    def run(self, _repository: Path) -> CheckResult:
        return CheckResult("fixture", True, "passed")


class FixtureProvider:
    def __init__(
        self,
        remote: Path,
        repository: Path,
        base_head: str,
        source_head: str,
        *,
        change_on_call: int | None = None,
    ) -> None:
        self.remote = remote
        self.repository = repository
        self.base_head = base_head
        self.source_head = source_head
        self.change_on_call = change_on_call
        self.get_calls = 0
        self.update_calls = 0

    def get_pull_request(self, repository: str, number: int) -> PullRequestSnapshot:
        assert repository == "owner/repo"
        assert number == 1
        self.get_calls += 1
        head = _git(self.remote, "rev-parse", "refs/heads/feature")
        if self.change_on_call == self.get_calls:
            head = "externally-updated-head"
        return PullRequestSnapshot(
            repository,
            number,
            "PULL_REQUEST_NODE",
            "https://github.com/owner/repo/pull/1",
            "OPEN",
            False,
            "feature",
            head,
            "master",
            self.base_head,
            "MERGEABLE",
            "CLEAN",
        )

    def update_source_branch(
        self,
        snapshot: PullRequestSnapshot,
        worktree: str | Path,
        expected_head: str,
        repaired_head: str,
    ) -> None:
        if snapshot.source_head != expected_head:
            raise RuntimeError("source head changed")
        _git(
            Path(worktree),
            "push",
            "--porcelain",
            f"--force-with-lease=refs/heads/feature:{expected_head}",
            "origin",
            f"{repaired_head}:refs/heads/feature",
        )
        self.update_calls += 1


class FixtureReviewProvider:
    def __init__(self, provider: FixtureProvider) -> None:
        self.provider = provider

    def get_pull_request(self, pull_request_id: str) -> PullRequestTarget:
        current = self.provider.get_pull_request("owner/repo", 1)
        return PullRequestTarget(pull_request_id, current.source_head or "")


class CommitAgent:
    def __init__(self) -> None:
        self.calls = 0
        self.prompt = ""
        self.cancel_calls = 0

    def execute_scoped_repair(
        self,
        problem_id: str,
        worktree: Path,
        prompt: str,
        model: str,
        cancelled: Callable[[], bool] | None = None,
    ) -> ModelExecution:
        del problem_id, model, cancelled
        self.calls += 1
        self.prompt = prompt
        (worktree / "fix.txt").write_text("fixed\n", encoding="utf-8")
        _git(worktree, "add", "fix.txt")
        _git(worktree, "commit", "-m", "repair selected finding")
        return ModelExecution(AttemptOutcome.SUCCESS, result="repair committed")

    def cancel(self, _problem_id: str) -> None:
        self.cancel_calls += 1


class BlockingAgent(CommitAgent):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()

    def execute_scoped_repair(
        self,
        problem_id: str,
        worktree: Path,
        prompt: str,
        model: str,
        cancelled: Callable[[], bool] | None = None,
    ) -> ModelExecution:
        del problem_id, worktree, model
        self.calls += 1
        self.prompt = prompt
        self.started.set()
        while cancelled is None or not cancelled():
            time.sleep(0.01)
        return ModelExecution(
            AttemptOutcome.FAILURE,
            failure_context="Agent stopped by operator",
        )


class RemoteChangingAgent(CommitAgent):
    def execute_scoped_repair(
        self,
        problem_id: str,
        worktree: Path,
        prompt: str,
        model: str,
        cancelled: Callable[[], bool] | None = None,
    ) -> ModelExecution:
        result = super().execute_scoped_repair(
            problem_id, worktree, prompt, model, cancelled
        )
        _git(worktree, "remote", "set-url", "--push", "origin", "owner/other.git")
        return result


@dataclass
class RepairHarness:
    repository: Path
    remote: Path
    source_head: str
    review_store: ReviewStore
    workflow_store: WorkflowStore
    routing_store: RoutingStore
    reviews: ReviewService
    workflow: WorkflowService
    provider: FixtureProvider
    agent: CommitAgent
    service: ReviewRepairService
    cycle_id: str
    selected_id: str
    unselected_id: str

    def close(self) -> None:
        self.review_store.close()
        self.workflow_store.close()
        self.routing_store.close()


def _harness(
    tmp_path: Path,
    agent: CommitAgent,
    *,
    change_on_call: int | None = None,
) -> RepairHarness:
    repository, remote, base_head, source_head = _repository(tmp_path)
    provider = FixtureProvider(
        remote,
        repository,
        base_head,
        source_head,
        change_on_call=change_on_call,
    )
    review_provider = FixtureReviewProvider(provider)
    review_store = ReviewStore(tmp_path / "reviews.db")
    reviews = ReviewService(review_store, provider=review_provider)
    cycle = reviews.start_cycle("owner/repo#1", source_head)
    snapshot = reviews.record_reader(
        cycle.cycle.cycle_id,
        ReviewConcern.SECURITY,
        ReaderStatus.FAIL,
        ("selected issue", "unselected issue"),
    )
    selected, unselected = snapshot.findings[-2:]
    with review_store.transaction() as connection:
        connection.executemany(
            "UPDATE review_findings SET publication_state = ? WHERE finding_id = ?",
            (
                (FindingPublicationState.PUBLISHED.value, selected.finding_id),
                (FindingPublicationState.PUBLISHED.value, unselected.finding_id),
            ),
        )
    workflow_store = WorkflowStore(tmp_path / "workflow.db")
    workflow = WorkflowService(
        workflow_store,
        repository,
        Constitution.load(CONSTITUTION_PATH),
        (PassingCheck(),),
    )
    routing_store = RoutingStore(tmp_path / "routing.db")
    service = ReviewRepairService(
        reviews,
        workflow,
        provider,  # type: ignore[arg-type]
        ModelRouter(routing_store),
        agent,  # type: ignore[arg-type]
        tmp_path / "repair-worktrees",
    )
    return RepairHarness(
        repository,
        remote,
        source_head,
        review_store,
        workflow_store,
        routing_store,
        reviews,
        workflow,
        provider,
        agent,
        service,
        cycle.cycle.cycle_id,
        selected.finding_id,
        unselected.finding_id,
    )


def test_selected_repair_pushes_one_committed_change_without_touching_checkout(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, CommitAgent())
    try:
        original_status = _git(harness.repository, "status", "--porcelain")
        attempt = harness.service.dispatch(
            harness.cycle_id, (harness.selected_id,), "operator"
        )
        completed = harness.service.wait(attempt.attempt_id, timeout=10)
        repeated = harness.service.dispatch(
            harness.cycle_id, (harness.selected_id,), "operator"
        )
        remote_head = _git(harness.remote, "rev-parse", "refs/heads/feature")

        assert completed.status is ReviewRepairStatus.SUCCEEDED
        assert completed.finding_ids == (harness.selected_id,)
        assert completed.head_sha == harness.source_head
        assert completed.actor == "operator"
        assert completed.commit_sha == remote_head
        assert completed.push_evidence is not None
        assert completed.push_evidence["pushed_head"] == remote_head
        assert harness.agent.calls == 1
        assert "selected issue" in harness.agent.prompt
        assert "unselected issue" not in harness.agent.prompt
        assert repeated.attempt_id == completed.attempt_id
        assert repeated.status is ReviewRepairStatus.SUCCEEDED
        assert harness.provider.update_calls == 1
        assert _git(harness.repository, "branch", "--show-current") == "feature"
        assert _git(harness.repository, "rev-parse", "HEAD") == harness.source_head
        assert _git(harness.repository, "status", "--porcelain") == original_status
        assert (
            harness.reviews.store.finding_for_id(harness.unselected_id).status.value
            == "open"
        )
    finally:
        harness.close()


def test_selected_repair_cancellation_stops_before_push(tmp_path: Path) -> None:
    agent = BlockingAgent()
    harness = _harness(tmp_path, agent)
    try:
        attempt = harness.service.dispatch(
            harness.cycle_id, (harness.selected_id,), "operator"
        )
        assert agent.started.wait(timeout=5)
        harness.service.cancel(attempt.attempt_id, "operator")
        completed = harness.service.wait(attempt.attempt_id, timeout=5)

        assert completed.status is ReviewRepairStatus.CANCELLED
        assert completed.cancellation_requested is True
        assert harness.provider.update_calls == 0
        assert _git(harness.remote, "rev-parse", "refs/heads/feature") == (
            harness.source_head
        )
        assert _git(harness.repository, "branch", "--show-current") == "feature"
    finally:
        harness.close()


def test_selected_repair_refuses_a_changed_source_head(tmp_path: Path) -> None:
    harness = _harness(tmp_path, CommitAgent(), change_on_call=4)
    try:
        attempt = harness.service.dispatch(
            harness.cycle_id, (harness.selected_id,), "operator"
        )
        completed = harness.service.wait(attempt.attempt_id, timeout=10)

        assert completed.status is ReviewRepairStatus.HUMAN_ACTION_REQUIRED
        assert "identity changed" in (completed.required_action or "")
        assert harness.provider.update_calls == 0
        assert _git(harness.remote, "rev-parse", "refs/heads/feature") == (
            harness.source_head
        )
        assert _git(harness.repository, "branch", "--show-current") == "feature"
        assert _git(harness.repository, "rev-parse", "HEAD") == harness.source_head
    finally:
        harness.close()


def test_selected_repair_refuses_a_changed_push_remote(tmp_path: Path) -> None:
    harness = _harness(tmp_path, RemoteChangingAgent())
    try:
        attempt = harness.service.dispatch(
            harness.cycle_id, (harness.selected_id,), "operator"
        )
        completed = harness.service.wait(attempt.attempt_id, timeout=10)

        assert completed.status is ReviewRepairStatus.HUMAN_ACTION_REQUIRED
        assert "push remote does not match" in (completed.required_action or "")
        assert harness.provider.update_calls == 0
        assert _git(harness.remote, "rev-parse", "refs/heads/feature") == (
            harness.source_head
        )
    finally:
        harness.close()


def test_repair_cancellation_winning_push_claim_prevents_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path, CommitAgent())
    original_begin = harness.review_store.begin_repair_push

    def cancel_before_claim(
        attempt_id: str,
        lease_id: str,
        commit_sha: str,
        push_evidence: dict[str, object],
    ) -> bool:
        harness.service.cancel(attempt_id, "operator")
        return original_begin(attempt_id, lease_id, commit_sha, push_evidence)

    monkeypatch.setattr(harness.review_store, "begin_repair_push", cancel_before_claim)
    try:
        attempt = harness.service.dispatch(
            harness.cycle_id, (harness.selected_id,), "operator"
        )
        completed = harness.service.wait(attempt.attempt_id, timeout=10)

        assert completed.status is ReviewRepairStatus.CANCELLED
        assert completed.cancellation_requested is True
        assert harness.provider.update_calls == 0
        assert _git(harness.remote, "rev-parse", "refs/heads/feature") == (
            harness.source_head
        )
    finally:
        harness.close()


def test_lease_loss_after_push_claim_prevents_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path, CommitAgent())
    original_begin = harness.review_store.begin_repair_push

    def lose_lease_after_claim(
        attempt_id: str,
        lease_id: str,
        commit_sha: str,
        push_evidence: dict[str, object],
    ) -> bool:
        started = original_begin(attempt_id, lease_id, commit_sha, push_evidence)
        assert started
        harness.workflow.store.stop_lease(lease_id, "simulated lease loss")
        return True

    monkeypatch.setattr(
        harness.review_store, "begin_repair_push", lose_lease_after_claim
    )
    try:
        attempt = harness.service.dispatch(
            harness.cycle_id, (harness.selected_id,), "operator"
        )
        completed = harness.service.wait(attempt.attempt_id, timeout=10)

        assert completed.status is ReviewRepairStatus.HUMAN_ACTION_REQUIRED
        assert "lease was lost" in (completed.required_action or "")
        assert harness.provider.update_calls == 0
        assert _git(harness.remote, "rev-parse", "refs/heads/feature") == (
            harness.source_head
        )
    finally:
        harness.close()


def test_repair_cannot_be_cancelled_after_push_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path, CommitAgent())
    original_begin = harness.review_store.begin_repair_push

    def reject_late_cancel(
        attempt_id: str,
        lease_id: str,
        commit_sha: str,
        push_evidence: dict[str, object],
    ) -> bool:
        started = original_begin(attempt_id, lease_id, commit_sha, push_evidence)
        with pytest.raises(ReviewError, match="after push starts"):
            harness.service.cancel(attempt_id, "operator")
        return started

    monkeypatch.setattr(harness.review_store, "begin_repair_push", reject_late_cancel)
    try:
        attempt = harness.service.dispatch(
            harness.cycle_id, (harness.selected_id,), "operator"
        )
        completed = harness.service.wait(attempt.attempt_id, timeout=10)

        assert completed.status is ReviewRepairStatus.SUCCEEDED
        assert completed.cancellation_requested is False
        assert harness.provider.update_calls == 1
    finally:
        harness.close()


def test_review_repair_recovers_a_queued_attempt_after_restart(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, CommitAgent())
    try:
        attempt, created = harness.reviews.create_repair_attempt(
            harness.cycle_id, (harness.selected_id,), "operator"
        )
        restarted = ReviewRepairService(
            harness.reviews,
            harness.workflow,
            harness.provider,  # type: ignore[arg-type]
            ModelRouter(harness.routing_store),
            harness.agent,  # type: ignore[arg-type]
            tmp_path / "repair-worktrees",
        )
        assert created is True
        assert attempt.status is ReviewRepairStatus.QUEUED

        restarted.recover()
        completed = restarted.wait(attempt.attempt_id, timeout=10)

        assert completed.status is ReviewRepairStatus.SUCCEEDED
        assert harness.agent.calls == 1
        repeated = restarted.dispatch(
            harness.cycle_id, (harness.selected_id,), "operator"
        )
        assert repeated.attempt_id == attempt.attempt_id
        assert harness.agent.calls == 1
    finally:
        harness.close()


def test_review_repair_marks_a_stopped_worker_as_actionable(tmp_path: Path) -> None:
    harness = _harness(tmp_path, CommitAgent())
    try:
        attempt, created = harness.reviews.create_repair_attempt(
            harness.cycle_id, (harness.selected_id,), "operator"
        )
        assert created is True
        assert harness.review_store.claim_repair_attempt(attempt.attempt_id)
        assert harness.review_store.attach_repair_lease(
            attempt.attempt_id, "missing-lease"
        )

        recovered = harness.service.get(attempt.attempt_id)

        assert recovered.status is ReviewRepairStatus.HUMAN_ACTION_REQUIRED
        assert "stopped before completion" in (recovered.required_action or "")
        assert harness.agent.calls == 0
    finally:
        harness.close()


@pytest.mark.parametrize("push_completed", [True, False])
def test_review_repair_recovers_an_interrupted_push(
    tmp_path: Path, push_completed: bool
) -> None:
    harness = _harness(tmp_path, CommitAgent())
    try:
        attempt, created = harness.reviews.create_repair_attempt(
            harness.cycle_id, (harness.selected_id,), "operator"
        )
        assert created is True
        if push_completed:
            repair_file = harness.repository / "recovered.txt"
            repair_file.write_text("recovered\n", encoding="utf-8")
            _git(harness.repository, "add", "recovered.txt")
            _git(harness.repository, "commit", "-m", "recovered repair")
            commit_sha = _git(harness.repository, "rev-parse", "HEAD")
            _git(
                harness.repository,
                "push",
                "origin",
                f"{commit_sha}:refs/heads/feature",
            )
        else:
            commit_sha = "f" * 40
        assert harness.review_store.claim_repair_attempt(attempt.attempt_id)
        assert harness.review_store.attach_repair_lease(
            attempt.attempt_id, "missing-lease"
        )
        evidence = {
            "repository": "owner/repo",
            "pull_request_number": 1,
            "source_branch": "feature",
            "expected_head": harness.source_head,
            "pushed_head": commit_sha,
        }
        assert harness.review_store.begin_repair_push(
            attempt.attempt_id, "missing-lease", commit_sha, evidence
        )

        recovered = harness.service.get(attempt.attempt_id)

        assert recovered.status is (
            ReviewRepairStatus.SUCCEEDED
            if push_completed
            else ReviewRepairStatus.HUMAN_ACTION_REQUIRED
        )
        assert recovered.commit_sha == commit_sha
        assert recovered.push_evidence == evidence
        assert harness.provider.update_calls == 0
    finally:
        harness.close()


def test_review_repair_api_enforces_operator_scope_and_demo_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = CommitAgent()
    harness = _harness(tmp_path, agent)
    try:
        writer_state = OrchestratorStore(":memory:")
        writer_routing = RoutingStore(":memory:")
        try:
            with TestClient(
                create_app(
                    store=writer_state,
                    routing_store=writer_routing,
                    review_service=harness.reviews,
                    review_repair_service=harness.service,
                    api_key="test-key",
                    review_actor="writer",
                )
            ) as writer_client:
                denied = writer_client.post(
                    f"/reviews/cycles/{harness.cycle_id}/repair",
                    json={"finding_ids": [harness.selected_id]},
                    headers={"X-API-Key": "test-key"},
                )
                assert denied.status_code == 409
                assert agent.calls == 0
        finally:
            writer_state.close()
            writer_routing.close()

        monkeypatch.setenv("BEEHAIIVE_REVIEW_MODE", "demo")
        demo_state = OrchestratorStore(":memory:")
        demo_routing = RoutingStore(":memory:")
        try:
            with TestClient(
                create_app(
                    store=demo_state,
                    routing_store=demo_routing,
                    review_service=harness.reviews,
                    review_repair_service=harness.service,
                    api_key="test-key",
                    review_actor="operator",
                )
            ) as demo_client:
                disabled = demo_client.post(
                    f"/reviews/cycles/{harness.cycle_id}/repair",
                    json={"finding_ids": [harness.selected_id]},
                    headers={"X-API-Key": "test-key"},
                )
                assert disabled.status_code == 503
                assert agent.calls == 0
        finally:
            demo_state.close()
            demo_routing.close()
    finally:
        harness.close()
