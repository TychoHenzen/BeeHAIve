import json
import sqlite3
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from conftest import FakeProvider

import beehaiive.agent as agent_module
from beehaiive.agent import (
    AgentWorkerManager,
    CodexExecModelExecutor,
    WorkerCapacityError,
)
from beehaiive.contracts import TaskContract, TaskOutcome, TaskResult
from beehaiive.demo import demo_review_adapters
from beehaiive.models import (
    PbiSnapshot,
    ProjectSnapshot,
    RepositorySnapshot,
    RunState,
    RunStatus,
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.review import REQUIRED_CONCERNS, ReaderStatus
from beehaiive.routing import (
    AttemptOutcome,
    ModelExecution,
    ModelRouter,
    ModelTier,
    RoutingStatus,
    RoutingStore,
)
from beehaiive.storage import OrchestratorStore, StoreError
from beehaiive.workflow import (
    CheckResult,
    Constitution,
    GitDeliveryResult,
    GitDeliveryStatus,
    LeaseStatus,
    WorkflowError,
    WorkflowService,
    WorkflowStore,
    WorkspaceLease,
)


class ScriptExecutor(CodexExecModelExecutor):
    def __init__(
        self, repository: Path, script: Path, timeout_seconds: float = 5
    ) -> None:
        super().__init__(
            repository,
            executable=sys.executable,
            timeout_seconds=timeout_seconds,
            repository_name="owner/api",
        )
        self.script = script

    def _command(self, prompt: str) -> list[str]:
        return [
            sys.executable,
            str(self.script),
            "--cd",
            str(self.repository),
            prompt,
        ]


class RepairScriptExecutor(CodexExecModelExecutor):
    def __init__(
        self, repository: Path, script: Path, timeout_seconds: float = 5
    ) -> None:
        super().__init__(
            repository,
            executable=sys.executable,
            timeout_seconds=timeout_seconds,
            repository_name="owner/api",
        )
        self.script = script

    def _repair_command(
        self, prompt: str, repository: Path, model: str | None = None
    ) -> list[str]:
        del model
        return [sys.executable, str(self.script), "--cd", str(repository), prompt]


class ImmediateExecutor(CodexExecModelExecutor):
    def __init__(self, result: ModelExecution, repository: Path | None = None) -> None:
        super().__init__(repository or Path.cwd(), repository_name="owner/api")
        self.result = result

    def execute(self, spec, decision) -> ModelExecution:
        del spec, decision
        return self.result


def agent_snapshot() -> ProjectSnapshot:
    return ProjectSnapshot(
        "project-1",
        "Agent demo",
        (
            RepositorySnapshot(
                "owner/api",
                (
                    PbiSnapshot(
                        "owner/api",
                        1,
                        "Demo PBI",
                        stage=Stage.REFINE,
                    ),
                ),
            ),
        ),
    )


def service_with_run(
    executor: CodexExecModelExecutor,
) -> tuple[Orchestrator, OrchestratorStore, RoutingStore, RunState]:
    store = OrchestratorStore()
    routing_store = RoutingStore()
    service = Orchestrator(
        store,
        FakeProvider(agent_snapshot()),
        ModelRouter(routing_store),
        executor,
    )
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    return service, store, routing_store, run


def make_git_repository(path: Path) -> Path:
    path.mkdir()
    for arguments in (
        ("init", "-b", "master"),
        ("config", "user.email", "tests@example.test"),
        ("config", "user.name", "Agent Tests"),
    ):
        result = subprocess.run(
            ("git", *arguments), cwd=path, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr or result.stdout
    (path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(("git", "add", "README.md"), cwd=path, check=True)
    subprocess.run(
        ("git", "commit", "-m", "base"), cwd=path, check=True, capture_output=True
    )
    return path


def configure_test_remote(repository: Path, root: Path) -> Path:
    remote = root / "owner" / "api.git"
    remote.parent.mkdir()
    subprocess.run(
        ("git", "init", "--bare", str(remote)), check=True, capture_output=True
    )
    subprocess.run(
        ("git", "remote", "add", "origin", str(remote)),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "push", "origin", "master"),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return remote


class PassingWorkflowCheck:
    name = "tests"

    def run(self, workspace: Path) -> CheckResult:
        assert workspace.exists()
        return CheckResult(self.name, True, "fixture passed")


def workflow_service_for(
    tmp_path: Path, repository: Path
) -> tuple[WorkflowService, WorkflowStore]:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(
        store,
        repository,
        Constitution.load(Path(__file__).parents[1] / "constitution.json"),
        [PassingWorkflowCheck()],
    )
    return service, store


def test_codex_executor_parses_final_message_without_passing_credentials(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("BEEHAIIVE_API_KEY", "operator-secret")
    script = tmp_path / "runner.py"
    script.write_text(
        "import json, os\n"
        "print(json.dumps({'type': 'item.completed', 'item': {"
        "'type': 'agent_message', 'text': 'inventory complete; token present='"
        "+ str('GITHUB_TOKEN' in os.environ)}}))\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {"
        "'input_tokens': 12, 'output_tokens': 7}}))\n",
        encoding="utf-8",
    )
    executor = ScriptExecutor(tmp_path, script)
    router = ModelRouter(RoutingStore())
    decision = router.begin("run-1").decision

    result = executor.execute(
        router.config.spec_for(ModelTier.LUNA),
        decision,
    )

    assert result.outcome is AttemptOutcome.SUCCESS
    assert result.result == "inventory complete; token present=False"
    assert result.input_tokens == 12
    assert result.output_tokens == 7


def test_codex_executor_streams_redacted_session_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "abc-secret")
    script = tmp_path / "session_runner.py"
    script.write_text(
        "import json\n"
        "print(json.dumps({'type': 'thread.started'}))\n"
        "print(json.dumps({'type': 'item.completed', 'item': {"
        "'type': 'agent_message', 'text': 'Bearer abc-secret'}}))\n"
        "print(json.dumps({'type': 'item.completed', 'item': {"
        "'type': 'command_execution', 'command': 'password=hidden'}}))\n"
        "print(json.dumps({'type': 'turn.completed'}))\n",
        encoding="utf-8",
    )
    executor = ScriptExecutor(tmp_path, script)
    events: list[tuple[str, str, str | None, str]] = []
    executor.set_session_event_handler("run-1", lambda *event: events.append(event))
    router = ModelRouter(RoutingStore())
    result = executor.execute(
        router.config.spec_for(ModelTier.LUNA), router.begin("run-1").decision
    )

    assert result.outcome is AttemptOutcome.SUCCESS
    assert events == [
        ("progress", "thread.started", None, "thread.started"),
        ("message", "item.completed", "assistant", "Bearer [redacted]"),
        ("progress", "item.completed", None, "item.completed"),
        ("progress", "turn.completed", None, "turn.completed"),
    ]
    executor.set_session_event_handler("run-1", None)
    assert "run-1" not in executor._session_event_handlers


def test_bounded_communicator_skips_oversized_lines_and_flushes_final_line() -> None:
    payload = json.dumps({"type": "turn.started"})
    script = (
        "import sys\n"
        "sys.stdout.write('x' * 64001 + '\\n')\n"
        "sys.stdout.write('y' * 100000 + '\\n')\n"
        f"sys.stdout.write({payload!r})\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    lines: list[str] = []

    _, _, timed_out = CodexExecModelExecutor._communicate_bounded(
        process, 5, lines.append
    )

    assert not timed_out
    assert lines == [payload]


def test_bounded_communicator_persists_flushed_event_before_process_exit(
    tmp_path: Path,
) -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, FakeProvider(agent_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease_token = run.lease_token or ""
    store.start_agent_session(run.run_id, "worker-1", "task", lease_token)
    executor = CodexExecModelExecutor(tmp_path, repository_name="owner/api")
    payload = json.dumps({"type": "thread.started"})
    script = f"import time\nprint({payload!r}, flush=True)\ntime.sleep(30)\n"
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    event_persisted = Event()
    outcome: list[tuple[str, str, bool]] = []

    def record_event(kind: str, source_type: str, role: str | None, text: str) -> None:
        store.record_agent_session_event(
            run.run_id, lease_token, kind, source_type, role, text
        )
        event_persisted.set()

    def communicate() -> None:
        def record_line(line: str) -> None:
            executor._record_session_line(line, record_event)

        outcome.append(
            CodexExecModelExecutor._communicate_bounded(process, 30, record_line)
        )

    communicator = Thread(target=communicate, daemon=True)
    communicator.start()
    try:
        assert event_persisted.wait(timeout=5)
        assert process.poll() is None
        pbi = store.project_state("project-1")["repositories"][0]["pbis"][0]
        session = pbi["agent_session"]
        assert session["state"] == "active"
        assert session["events"][0]["text"] == "thread.started"
    finally:
        if process.poll() is None:
            process.kill()
        communicator.join(timeout=5)
        store.close()
    assert not communicator.is_alive()
    assert len(outcome) == 1


def test_bounded_communicator_propagates_session_write_failures() -> None:
    def fail_write(_line: str) -> None:
        raise RuntimeError("session persistence failed")

    for script in (
        "print('session event')",
        "import sys; sys.stdout.write('session event')",
    ):
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        with pytest.raises(RuntimeError, match="session persistence failed"):
            CodexExecModelExecutor._communicate_bounded(process, 5, fail_write)


def test_session_event_parser_ignores_non_message_payloads(tmp_path: Path) -> None:
    executor = CodexExecModelExecutor(tmp_path, repository_name="owner/api")
    events: list[tuple[str, str, str | None, str]] = []

    def record(kind: str, source: str, role: str | None, text: str) -> None:
        events.append((kind, source, role, text))

    executor._record_session_line("not json", record)
    assert executor._session_event("[]") is None
    assert executor._session_event(
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "reasoning", "text": "private reasoning"},
            }
        )
    ) == ("progress", "item.completed", None, "item.completed")
    assert (
        executor._session_event(json.dumps({"type": "custom.event", "text": "ignored"}))
        is None
    )
    executor._record_session_line(
        json.dumps({"type": "agent_message", "text": "top-level message"}), record
    )

    assert events == [("message", "agent_message", "assistant", "top-level message")]


def test_codex_executor_validates_structured_task_results(
    tmp_path: Path,
) -> None:
    script = tmp_path / "structured_runner.py"
    script.write_text(
        "import json\n"
        "print(json.dumps({'type': 'agent_message', 'text': json.dumps({"
        "'outcome': 'pass', 'evidence': {'summary': 'inventory complete'}, "
        "'artifact_refs': []})}))\n",
        encoding="utf-8",
    )
    executor = ScriptExecutor(tmp_path, script)
    contract = TaskContract.inventory("owner/api", 1, "Demo")
    executor.prepare_run("structured-1", "owner/api")
    executor.set_task_contract("structured-1", contract)
    try:
        router = ModelRouter(RoutingStore())
        result = executor.execute(
            router.config.spec_for(ModelTier.LUNA),
            router.begin("structured-1").decision,
        )
        assert result.outcome is AttemptOutcome.SUCCESS
        assert result.task_result is not None
        assert result.task_result.outcome.value == "pass"
    finally:
        executor.release_run("structured-1")

    invalid_script = tmp_path / "invalid_structured_runner.py"
    invalid_script.write_text(
        "import json\n"
        "print(json.dumps({'type': 'agent_message', 'text': 'not-json'}))\n",
        encoding="utf-8",
    )
    invalid_executor = ScriptExecutor(tmp_path, invalid_script)
    invalid_executor.prepare_run("structured-2", "owner/api")
    invalid_executor.set_task_contract("structured-2", contract)
    try:
        router = ModelRouter(RoutingStore())
        result = invalid_executor.execute(
            router.config.spec_for(ModelTier.LUNA),
            router.begin("structured-2").decision,
        )
        assert result.outcome is AttemptOutcome.SUCCESS
        assert result.task_result is not None
        assert result.task_result.outcome is TaskOutcome.FAIL
        assert result.task_result.validation_reason is not None
    finally:
        invalid_executor.release_run("structured-2")


def test_codex_executor_terminates_timed_out_process_tree(tmp_path: Path) -> None:
    script = tmp_path / "slow_runner.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    executor = ScriptExecutor(tmp_path, script, timeout_seconds=0.1)
    router = ModelRouter(RoutingStore())
    decision = router.begin("run-2").decision

    started = time.monotonic()
    result = executor.execute(
        router.config.spec_for(ModelTier.LUNA),
        decision,
    )

    assert result.outcome is AttemptOutcome.FAILURE
    assert "timed out" in result.failure_context
    assert time.monotonic() - started < 5


def test_codex_executor_repair_writes_only_the_leased_worktree(
    tmp_path: Path,
) -> None:
    script = tmp_path / "repair_runner.py"
    script.write_text(
        "import json, os\n"
        "open('repair-output.txt', 'w').write('inside')\n"
        "print(json.dumps({'type': 'agent_message', 'text': 'repair done; token=' + "
        "str('GITHUB_TOKEN' in os.environ)}))\n",
        encoding="utf-8",
    )
    worktree = tmp_path / "repair-worktree"
    worktree.mkdir()
    executor = RepairScriptExecutor(tmp_path, script)

    result = executor.execute_repair("repair-1", worktree, "feature", "master")

    assert result.outcome is AttemptOutcome.SUCCESS
    assert result.result == "repair done; token=[redacted]"
    assert (worktree / "repair-output.txt").read_text(encoding="utf-8") == "inside"
    command = CodexExecModelExecutor(
        tmp_path, model="repair-model", repository_name="owner/api"
    )._repair_command("prompt", worktree)
    assert "workspace-write" in command
    assert "--strict-config" in command
    assert "sandbox_workspace_write.network_access=false" in command
    assert "sandbox_workspace_write.exclude_slash_tmp=true" in command
    assert "sandbox_workspace_write.exclude_tmpdir_env_var=true" in command
    assert "agents.enabled=false" in command
    assert str(worktree) in command
    routed_command = CodexExecModelExecutor(tmp_path)._repair_command(
        "prompt", worktree, "routed-model"
    )
    assert routed_command[routed_command.index("--model") + 1] == "routed-model"


def test_codex_executor_repair_handles_invalid_launch_failure_and_empty_result(
    tmp_path: Path,
) -> None:
    executor = RepairScriptExecutor(tmp_path, tmp_path / "unused.py")
    missing = executor.execute_repair(
        "missing-worktree", tmp_path / "missing", "feature", "master"
    )
    assert missing.outcome is AttemptOutcome.FAILURE
    assert "does not exist" in missing.failure_context

    failed_script = tmp_path / "repair-failed.py"
    failed_script.write_text("print('token=visible-secret')\nraise SystemExit(3)\n")
    failed = RepairScriptExecutor(tmp_path, failed_script).execute_repair(
        "repair-failed", tmp_path, "feature", "master"
    )
    assert failed.outcome is AttemptOutcome.FAILURE
    assert "Bounded repair agent failed" in failed.failure_context
    assert "visible-secret" not in failed.failure_context
    assert "token=[redacted]" in failed.failure_context

    empty_script = tmp_path / "repair-empty.py"
    empty_script.write_text("pass\n")
    empty = RepairScriptExecutor(tmp_path, empty_script).execute_repair(
        "repair-empty", tmp_path, "feature", "master"
    )
    assert empty.outcome is AttemptOutcome.SUCCESS
    assert "completed" in empty.result


def test_codex_executor_repair_honors_cancellation_and_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancelled = RepairScriptExecutor(tmp_path, tmp_path / "unused.py")
    cancelled.prepare_run("repair-cancel-before", "owner/api")
    cancelled.cancel("repair-cancel-before")
    result = cancelled.execute_repair(
        "repair-cancel-before", tmp_path, "feature", "master"
    )
    assert result.outcome is AttemptOutcome.FAILURE
    assert "stopped" in result.failure_context

    slow_script = tmp_path / "repair-slow.py"
    slow_script.write_text("import time\ntime.sleep(30)\n")
    slow = RepairScriptExecutor(tmp_path, slow_script, timeout_seconds=0.1)
    timed_out = slow.execute_repair("repair-timeout", tmp_path, "feature", "master")
    assert timed_out.outcome is AttemptOutcome.FAILURE
    assert "timed out" in timed_out.failure_context

    class CancelOnStartExecutor(RepairScriptExecutor):
        def _start_process(
            self, command: list[str], environment: dict[str, str]
        ) -> subprocess.Popen[str]:
            process = super()._start_process(command, environment)
            self.cancel("repair-cancel-launch")
            return process

    cancelled_after_start = CancelOnStartExecutor(tmp_path, slow_script)
    stopped = cancelled_after_start.execute_repair(
        "repair-cancel-launch", tmp_path, "feature", "master"
    )
    assert stopped.outcome is AttemptOutcome.FAILURE
    assert "stopped" in stopped.failure_context

    timeout_exception = RepairScriptExecutor(tmp_path, slow_script)
    monkeypatch.setattr(
        timeout_exception,
        "_start_process",
        lambda command, environment: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        timeout_exception,
        "_communicate_bounded",
        lambda process, timeout: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("codex", timeout)
        ),
    )
    monkeypatch.setattr(timeout_exception, "_terminate_process", lambda process: None)
    raised_timeout = timeout_exception.execute_repair(
        "repair-timeout-exception", tmp_path, "feature", "master"
    )
    assert raised_timeout.outcome is AttemptOutcome.FAILURE
    assert "timed out" in raised_timeout.failure_context


def test_executor_validates_configuration_and_reads_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        CodexExecModelExecutor(tmp_path / "missing")
    with pytest.raises(ValueError, match="executable is required"):
        CodexExecModelExecutor(tmp_path, executable=" ")
    with pytest.raises(ValueError, match="timeout must be positive"):
        CodexExecModelExecutor(tmp_path, timeout_seconds=0)
    with pytest.raises(ValueError, match="task is required"):
        CodexExecModelExecutor(tmp_path, task=" ")

    monkeypatch.setenv("BEEHAIIVE_AGENT_TIMEOUT_SECONDS", "invalid")
    with pytest.raises(ValueError, match="must be a positive number"):
        CodexExecModelExecutor.from_environment()

    monkeypatch.setenv("BEEHAIIVE_AGENT_TIMEOUT_SECONDS", "3.5")
    monkeypatch.setenv("BEEHAIIVE_AGENT_REPOSITORY", str(tmp_path))
    monkeypatch.setenv("BEEHAIIVE_CODEX_EXECUTABLE", " codex-test ")
    monkeypatch.setenv("BEEHAIIVE_CODEX_MODEL", " luna-test ")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("BEEHAIIVE_API_KEY", "operator-secret")
    executor = CodexExecModelExecutor.from_environment()

    assert executor.repository == tmp_path.resolve()
    assert executor.executable == "codex-test"
    assert executor.timeout_seconds == 3.5
    assert executor.model == "luna-test"
    safe_environment = executor._safe_environment()
    assert "GITHUB_TOKEN" not in safe_environment
    assert "BEEHAIIVE_API_KEY" not in safe_environment
    monkeypatch.setenv("DATABASE_URL", "postgres://secret")
    monkeypatch.setenv("PIP_INDEX_URL", "https://secret.example")
    safe_environment = executor._safe_environment()
    assert "DATABASE_URL" not in safe_environment
    assert "PIP_INDEX_URL" not in safe_environment
    assert "PATH" in safe_environment

    monkeypatch.setenv("BEEHAIIVE_AGENT_TIMEOUT_SECONDS", "nan")
    with pytest.raises(ValueError, match="finite"):
        CodexExecModelExecutor.from_environment()
    monkeypatch.setenv("BEEHAIIVE_AGENT_TIMEOUT_SECONDS", "inf")
    with pytest.raises(ValueError, match="finite"):
        CodexExecModelExecutor.from_environment()


def test_executor_binds_repository_and_uses_selected_model(tmp_path: Path) -> None:
    executor = CodexExecModelExecutor(
        tmp_path, model="configured-model", repository_name="owner/api"
    )
    with pytest.raises(StoreError, match="identity is not configured"):
        CodexExecModelExecutor(tmp_path).validate_repository("owner/api")
    with pytest.raises(StoreError, match="does not match"):
        executor.validate_repository("owner/other")
    command = executor._command_for_execution("prompt", "selected-model", tmp_path)
    assert command[-3:] == ["--model", "selected-model", "prompt"]

    default_command = CodexExecModelExecutor(
        tmp_path, repository_name="owner/api"
    )._command_for_execution("prompt", "luna", tmp_path)
    assert "--model" not in default_command


def test_writer_command_locks_sandbox_network_and_agent_policy(
    tmp_path: Path,
) -> None:
    executor = CodexExecModelExecutor(
        tmp_path, repository_name="owner/api", model="writer-model"
    )

    command = executor._workspace_command_for_execution(
        "implement", "writer-model", tmp_path
    )

    assert "workspace-write" in command
    assert "--strict-config" in command
    assert "sandbox_workspace_write.network_access=false" in command
    assert "sandbox_workspace_write.exclude_slash_tmp=true" in command
    assert "sandbox_workspace_write.exclude_tmpdir_env_var=true" in command
    assert "agents.enabled=false" in command
    assert "--add-dir" not in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert command[command.index("--cd") + 1] == str(tmp_path)


def test_executor_rejects_missing_and_lost_workspace_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    executor = CodexExecModelExecutor(repository, repository_name="owner/api")
    routing_store = RoutingStore()
    router = ModelRouter(routing_store)
    decision = router.begin("leased-validation").decision
    spec = router.config.spec_for(ModelTier.LUNA)
    lease = WorkspaceLease(
        "lease-1",
        "dashboard-run:leased-validation",
        "codex/leased-validation",
        str(repository),
        LeaseStatus.ACTIVE,
        "created",
        "updated",
        "lease-token",
    )
    executor._workspace_leases[decision.problem_id] = lease

    try:
        missing_validator = executor.execute(spec, decision)
        assert missing_validator.outcome is AttemptOutcome.FAILURE
        assert "validator is missing" in (missing_validator.failure_context or "")

        executor._workspace_validators[decision.problem_id] = lambda: None
        executor._workspace_leases[decision.problem_id] = replace(
            lease, worktree_path=str(tmp_path / "missing-worktree")
        )
        missing_worktree = executor.execute(spec, decision)
        assert missing_worktree.outcome is AttemptOutcome.FAILURE
        assert "worktree does not exist" in (missing_worktree.failure_context or "")

        executor._workspace_leases[decision.problem_id] = lease
        validations = 0

        def lose_lease_after_launch() -> None:
            nonlocal validations
            validations += 1
            if validations == 2:
                raise WorkflowError("lease changed")

        executor._workspace_validators[decision.problem_id] = lose_lease_after_launch
        monkeypatch.setattr(
            executor,
            "_workspace_command_for_execution",
            lambda _prompt, _model, worktree: [
                sys.executable,
                "-c",
                "print('{}')",
                "--cd",
                str(worktree),
            ],
        )
        lost_lease = executor.execute(spec, decision)
        assert lost_lease.outcome is AttemptOutcome.FAILURE
        assert "lease changed" in (lost_lease.failure_context or "")
    finally:
        executor.release_run(decision.problem_id)
        routing_store.close()


def test_prepare_run_rejects_invalid_and_credentialed_workspaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)
    lease = workflow_service.acquire_workspace(
        "prepare-test", "codex/prepare-test", tmp_path / "prepare-worktree"
    )
    executor = CodexExecModelExecutor(repository, repository_name="owner/api")

    def validate() -> None:
        return None

    try:
        with pytest.raises(StoreError, match="active workflow workspace lease"):
            executor.prepare_run(
                "prepare-test",
                "owner/api",
                replace(lease, status=LeaseStatus.RETAINED),
                validate,
            )
        with pytest.raises(StoreError, match="lease token is required"):
            executor.prepare_run(
                "prepare-test", "owner/api", replace(lease, lease_token=None), validate
            )
        with pytest.raises(StoreError, match="lease token is required"):
            executor.prepare_run("prepare-test", "owner/api", lease)
        with pytest.raises(StoreError, match="worktree is unavailable"):
            executor.prepare_run(
                "prepare-test",
                "owner/api",
                replace(lease, worktree_path=str(tmp_path / "missing-worktree")),
                validate,
            )

        monkeypatch.setattr(executor, "_repository_files", lambda: (Path(".env"),))
        with pytest.raises(StoreError, match="Credential-like tracked files"):
            executor.prepare_run("prepare-test", "owner/api", lease, validate)
    finally:
        workflow_service.discard_workspace(lease.lease_id, "test complete")
        workflow_store.close()


def test_executor_prompt_contains_verified_repository_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = CodexExecModelExecutor(tmp_path, repository_name="owner/api")
    executor.set_session_task("prompt-metadata", "persisted executable task")
    monkeypatch.setattr(executor, "_discover_repository_branch", lambda: "feature/demo")
    monkeypatch.setattr(
        executor,
        "_repository_files",
        lambda: (Path("README.md"), Path("main.py")),
    )

    router = ModelRouter(RoutingStore())
    spec = router.config.spec_for(ModelTier.LUNA)
    prompt = executor._prompt(spec, router.begin("prompt-metadata").decision)
    default_prompt = executor._prompt(spec, router.begin("default-task").decision)

    assert "Verified current branch: feature/demo" in prompt
    assert "Verified tracked file count: 2" in prompt
    assert "Task: persisted executable task" in prompt
    assert ".git" in default_prompt


def test_executor_safe_checkout_excludes_local_secret_files(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("safe\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (tmp_path / ".env.production").write_text("TOKEN=secret\n", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("TOKEN=\n", encoding="utf-8")
    (tmp_path / "id_rsa").write_text("private key\n", encoding="utf-8")
    (tmp_path / "id_ed25519").write_text("private key\n", encoding="utf-8")
    (tmp_path / "service-account.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "application.pem").write_text("private key\n", encoding="utf-8")
    (tmp_path / "application.p12").write_text("private key\n", encoding="utf-8")
    executor = CodexExecModelExecutor(tmp_path, repository_name="owner/api")

    with executor._safe_checkout() as checkout:
        assert (checkout / "README.md").is_file()
        assert (checkout / ".env.example").is_file()
        assert not (checkout / ".env").exists()
        assert not (checkout / ".env.production").exists()
        assert not (checkout / "secret.txt").exists()
        assert not (checkout / "id_rsa").exists()
        assert not (checkout / "id_ed25519").exists()
        assert not (checkout / "service-account.json").exists()
        assert not (checkout / "application.pem").exists()
        assert not (checkout / "application.p12").exists()


def test_executor_repository_discovery_handles_git_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_run = agent_module.subprocess.run

    def git_run(command, **kwargs):
        if command[-3:] == ["config", "--get", "remote.origin.url"]:
            return SimpleNamespace(stdout="git@github.com:owner/api.git", returncode=0)
        return original_run(command, **kwargs)

    monkeypatch.setattr(agent_module.subprocess, "run", git_run)
    executor = CodexExecModelExecutor(tmp_path)
    assert executor.repository_name == "owner/api"

    def broken_run(*args, **kwargs):
        del args, kwargs
        raise OSError("git unavailable")

    monkeypatch.setattr(agent_module.subprocess, "run", broken_run)
    assert CodexExecModelExecutor(tmp_path).repository_name is None
    assert (
        CodexExecModelExecutor(
            tmp_path, repository_name="owner/api"
        )._discover_repository_branch()
        == "unknown"
    )


def test_executor_handles_communicate_timeout_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = ScriptExecutor(tmp_path, tmp_path / "unused.py")
    router = ModelRouter(RoutingStore())

    monkeypatch.setattr(
        executor, "_start_process", lambda command, environment: object()
    )

    def raise_timeout(process, timeout):
        del process, timeout
        raise subprocess.TimeoutExpired("codex", 1)

    monkeypatch.setattr(executor, "_communicate_bounded", raise_timeout)
    monkeypatch.setattr(
        CodexExecModelExecutor,
        "_terminate_process",
        staticmethod(lambda process: None),
    )

    result = executor.execute(
        router.config.spec_for(ModelTier.LUNA),
        router.begin("communicate-timeout").decision,
    )

    assert result.outcome is AttemptOutcome.FAILURE
    assert "timed out" in result.failure_context


def test_executor_safe_checkout_skips_unsafe_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path.parent / "outside-agent-file.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (tmp_path / "directory").mkdir()
    executor = CodexExecModelExecutor(tmp_path, repository_name="owner/api")
    monkeypatch.setattr(
        executor,
        "_repository_files",
        lambda: (Path("../outside-agent-file.txt"), Path("directory")),
    )

    with executor._safe_checkout() as checkout:
        assert not (checkout / "outside-agent-file.txt").exists()
        assert not (checkout / "directory").exists()


def test_executor_repository_file_discovery_uses_git_and_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = CodexExecModelExecutor(tmp_path, repository_name="owner/api")

    def git_timeout(*args, **kwargs):
        del args, kwargs
        raise subprocess.TimeoutExpired("git", 5)

    monkeypatch.setattr(agent_module.subprocess, "run", git_timeout)
    (tmp_path / "fallback.py").write_text("pass\n", encoding="utf-8")
    assert executor._repository_files() == (Path("fallback.py"),)

    monkeypatch.setattr(
        agent_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=b"one.py\0nested/two.py\0"
        ),
    )
    assert executor._repository_files() == (Path("one.py"), Path("nested/two.py"))


def test_executor_honors_cancellation_before_and_during_launch(
    tmp_path: Path,
) -> None:
    router = ModelRouter(RoutingStore())
    executor = ScriptExecutor(tmp_path, tmp_path / "unused.py")
    executor.prepare_run("cancel-before-launch", "owner/api")
    executor.cancel("cancel-before-launch")
    result = executor.execute(
        router.config.spec_for(ModelTier.LUNA),
        router.begin("cancel-before-launch").decision,
    )
    assert result.outcome is AttemptOutcome.FAILURE
    assert "stopped" in result.failure_context

    script = tmp_path / "slow_runner.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

    class CancelOnStartExecutor(ScriptExecutor):
        def _start_process(self, command, environment):
            process = super()._start_process(command, environment)
            self.cancel("cancel-on-start")
            return process

    early_cancel = CancelOnStartExecutor(tmp_path, script, timeout_seconds=5)
    early_result = early_cancel.execute(
        router.config.spec_for(ModelTier.LUNA),
        router.begin("cancel-on-start").decision,
    )
    assert early_result.outcome is AttemptOutcome.FAILURE
    assert "stopped" in early_result.failure_context

    running = ScriptExecutor(tmp_path, script, timeout_seconds=5)
    holder: dict[str, ModelExecution] = {}

    def execute() -> None:
        holder["result"] = running.execute(
            router.config.spec_for(ModelTier.LUNA),
            router.begin("cancel-running").decision,
        )

    thread = Thread(target=execute)
    thread.start()
    deadline = time.monotonic() + 3
    while "cancel-running" not in running._processes:
        if time.monotonic() >= deadline:
            raise AssertionError("The test process did not start")
        time.sleep(0.01)
    running.cancel("cancel-running")
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert holder["result"].outcome is AttemptOutcome.FAILURE
    assert "stopped" in holder["result"].failure_context


def test_executor_handles_nonzero_and_empty_output(tmp_path: Path) -> None:
    router = ModelRouter(RoutingStore())
    failed_script = tmp_path / "failed_runner.py"
    failed_script.write_text(
        "print('token=visible-secret')\nraise SystemExit(3)\n",
        encoding="utf-8",
    )
    failed = ScriptExecutor(tmp_path, failed_script).execute(
        router.config.spec_for(ModelTier.LUNA),
        router.begin("nonzero").decision,
    )
    assert failed.outcome is AttemptOutcome.FAILURE
    assert "Bounded agent failed" in failed.failure_context
    assert "token=[redacted]" in failed.failure_context

    empty_script = tmp_path / "empty_runner.py"
    empty_script.write_text("pass\n", encoding="utf-8")
    empty = ScriptExecutor(tmp_path, empty_script).execute(
        router.config.spec_for(ModelTier.LUNA),
        router.begin("empty").decision,
    )
    assert empty.outcome is AttemptOutcome.FAILURE
    assert "no result" in empty.failure_context

    silent_script = tmp_path / "silent_failure.py"
    silent_script.write_text("raise SystemExit(4)\n", encoding="utf-8")
    silent = ScriptExecutor(tmp_path, silent_script).execute(
        router.config.spec_for(ModelTier.LUNA),
        router.begin("silent-failure").decision,
    )
    assert silent.outcome is AttemptOutcome.FAILURE
    assert "status 4" in silent.failure_context


def test_executor_parses_fallback_messages_and_invalid_usage() -> None:
    parsed = CodexExecModelExecutor._parse_output(
        "\n".join(
            (
                "not json",
                json.dumps(["not an event"]),
                json.dumps(
                    {
                        "type": "agent_message",
                        "content": [
                            {"text": "first"},
                            "ignored",
                            {"text": 3},
                            {"text": "second"},
                        ],
                        "usage": {"input_tokens": -1, "output_tokens": True},
                    }
                ),
            )
        )
    )
    assert parsed[0] == "first\nsecond"
    assert parsed[1] >= 1
    assert parsed[2] >= 1


def test_executor_builds_model_command_and_covers_process_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = CodexExecModelExecutor(tmp_path, model="model-x")
    command = executor._command("prompt")
    assert command[-3:] == ["--model", "model-x", "prompt"]
    assert "--sandbox" in command
    assert "--skip-git-repo-check" in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert "--approve-for-me" in command
    assert executor._command("prompt")[-1] == "prompt"

    captured: dict[str, object] = {}

    class StartedProcess:
        pass

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return StartedProcess()

    monkeypatch.setattr(agent_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(agent_module.os, "name", "posix")
    started = CodexExecModelExecutor._start_process(
        [sys.executable, "-c", "pass", "--cd", str(tmp_path)], {}
    )
    assert isinstance(started, StartedProcess)
    assert captured["start_new_session"] is True

    monkeypatch.setattr(agent_module.os, "name", "nt")
    monkeypatch.setattr(
        agent_module.subprocess,
        "CREATE_NEW_PROCESS_GROUP",
        0x200,
        raising=False,
    )
    captured.pop("start_new_session")
    started = CodexExecModelExecutor._start_process(
        [sys.executable, "-c", "pass", "--cd", str(tmp_path)], {}
    )
    assert isinstance(started, StartedProcess)
    assert captured["creationflags"] == 0x200
    assert "start_new_session" not in captured
    monkeypatch.setattr(agent_module.os, "name", "posix")
    monkeypatch.setattr(agent_module.signal, "SIGTERM", 15, raising=False)
    monkeypatch.setattr(agent_module.signal, "SIGKILL", 9, raising=False)

    class Process:
        pid = 2

        def __init__(self, running: bool = True) -> None:
            self.running = running
            self.terminated = False
            self.killed = False
            self.waits = 0

        def poll(self):
            return None if self.running else 0

        def terminate(self):
            self.terminated = True
            self.running = False

        def kill(self):
            self.killed = True
            self.running = False

        def wait(self, timeout=None):
            del timeout
            self.waits += 1
            self.running = False

    clock_value = [0.0]

    def monotonic() -> float:
        current = clock_value[0]
        clock_value[0] += 0.5
        return current

    def sleep(seconds: float) -> None:
        clock_value[0] += seconds

    monkeypatch.setattr(agent_module.time, "monotonic", monotonic)
    monkeypatch.setattr(agent_module.time, "sleep", sleep)
    process_group_alive = [True]
    group_signals: list[int] = []

    def kill_group(group: int, sig: int) -> None:
        assert group == 2
        if sig == 0:
            if not process_group_alive[0]:
                raise ProcessLookupError("process group exited")
            return
        group_signals.append(sig)
        if sig == 9:
            process_group_alive[0] = False

    monkeypatch.setattr(agent_module.os, "killpg", kill_group, raising=False)
    leader_exited = Process()
    CodexExecModelExecutor._terminate_process(leader_exited)
    assert group_signals == [15, 9]
    assert not process_group_alive[0]
    assert not leader_exited.terminated

    def missing_group(_group: int, _sig: int) -> None:
        raise ProcessLookupError("process group exited")

    monkeypatch.setattr(agent_module.os, "killpg", missing_group, raising=False)
    missing_group_process = Process()
    CodexExecModelExecutor._terminate_process(missing_group_process)
    assert missing_group_process.terminated

    finished_process = Process(running=False)
    CodexExecModelExecutor._terminate_process(finished_process)
    assert finished_process.waits == 0

    clock_value[0] = 0
    stubborn_group_alive = [True]

    def stubborn_group(_group: int, sig: int) -> None:
        if sig == 0:
            if not stubborn_group_alive[0]:
                raise ProcessLookupError("process group exited")
        elif sig == 9:
            stubborn_group_alive[0] = False

    monkeypatch.setattr(agent_module.os, "killpg", stubborn_group, raising=False)

    class StuckProcess(Process):
        def wait(self, timeout=None):
            self.waits += 1
            raise subprocess.TimeoutExpired("process", timeout)

    stuck_process = StuckProcess()
    CodexExecModelExecutor._terminate_process(stuck_process)
    assert stuck_process.killed
    assert stuck_process.waits == 4

    monkeypatch.setattr(agent_module.os, "name", "nt")
    taskkill_calls = []

    def taskkill(command, **kwargs):
        taskkill_calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(agent_module.subprocess, "run", taskkill)
    windows_process = Process()
    CodexExecModelExecutor._terminate_process(windows_process)
    assert taskkill_calls[0][0] == ["taskkill", "/PID", "2", "/T", "/F"]
    assert taskkill_calls[0][1]["check"] is True
    assert windows_process.waits == 1

    class WindowsProcessTimeoutOnce(Process):
        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("process", timeout)
            self.running = False

    taskkill_calls.clear()
    timeout_windows_process = WindowsProcessTimeoutOnce()
    CodexExecModelExecutor._terminate_process(timeout_windows_process)
    assert len(taskkill_calls) == 2
    assert timeout_windows_process.waits == 2

    finished_windows_process = Process(running=False)
    CodexExecModelExecutor._terminate_process(finished_windows_process)
    assert finished_windows_process.waits == 0

    def failed_taskkill(command, **kwargs):
        del kwargs
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(agent_module.subprocess, "run", failed_taskkill)
    failed_windows_process = Process()
    with pytest.raises(subprocess.CalledProcessError):
        CodexExecModelExecutor._terminate_process(failed_windows_process)
    assert not failed_windows_process.killed


def test_executor_bounds_stdout_and_stderr_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Stream:
        def __init__(self, chunks) -> None:
            self.chunks = iter(chunks)
            self.closed = False

        def read(self, size):
            del size
            return next(self.chunks, b"")

        def read1(self, size):
            return self.read(size)

        def close(self) -> None:
            self.closed = True

    class FailingStream(Stream):
        def read(self, size):
            del size
            raise OSError("pipe closed")

    class Process:
        stdout = Stream(["x" * 100_000, ""])
        stderr = FailingStream([])

        def wait(self, timeout=None):
            del timeout

    stdout, stderr, timed_out = CodexExecModelExecutor._communicate_bounded(
        Process(), 1
    )
    assert len(stdout) == agent_module.MAX_AGENT_OUTPUT_BYTES
    assert stderr == ""
    assert timed_out is False

    class EmptyProcess:
        stdout = None
        stderr = None

        def wait(self, timeout=None):
            del timeout

    assert CodexExecModelExecutor._communicate_bounded(EmptyProcess(), 1) == (
        "",
        "",
        False,
    )

    class CloseTrackingStream:
        def __init__(self) -> None:
            self.closed = False

        def read(self, size):
            del size
            return b"ignored"

        def read1(self, size):
            return self.read(size)

        def close(self) -> None:
            self.closed = True

    stream = CloseTrackingStream()

    class OneStuckReader:
        def __init__(self, target, args, name, daemon) -> None:
            del target, name, daemon
            self.args = args

        def start(self) -> None:
            return None

        def join(self, timeout=None) -> None:
            del timeout

        def is_alive(self) -> bool:
            return True

    class OneStreamProcess:
        stdout = stream
        stderr = None

        def wait(self, timeout=None) -> None:
            del timeout

    monkeypatch.setattr(agent_module, "Thread", OneStuckReader)
    CodexExecModelExecutor._communicate_bounded(OneStreamProcess(), 1)
    assert stream.closed is True


def test_executor_text_parser_rejects_unsupported_values() -> None:
    assert agent_module._text_value(3) == ""


def test_agent_session_round_trips_through_project_state(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    store = OrchestratorStore(database)
    service = Orchestrator(store, FakeProvider(agent_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease_token = run.lease_token or ""

    store.start_agent_session(
        run.run_id, "agent-process-1", "bounded repository inventory", lease_token
    )
    store.record_agent_session_event(
        run.run_id, lease_token, "progress", "turn.started", None, "turn started"
    )
    store.record_agent_session_event(
        run.run_id,
        lease_token,
        "message",
        "item.completed",
        "assistant",
        "Repository inventory is ready.",
    )

    session = store.get_agent_session(run.run_id)
    assert session is not None
    assert session["session_id"] == run.run_id
    assert session["worker_id"] == "agent-process-1"
    assert session["task"] == "bounded repository inventory"
    assert session["state"] == "active"
    assert [event["sequence"] for event in session["events"]] == [1, 2]

    pbi = store.project_state("project-1")["repositories"][0]["pbis"][0]
    assert pbi["agent_session"] == session
    store.close()

    reopened = OrchestratorStore(database)
    persisted = reopened.get_agent_session(run.run_id)
    assert persisted == session
    reopened.close()


def test_agent_session_rejects_invalid_start_and_event_requests(
    tmp_path: Path,
) -> None:
    store = OrchestratorStore(tmp_path / "state.sqlite3")
    service = Orchestrator(store, FakeProvider(agent_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease_token = run.lease_token or ""

    with pytest.raises(StoreError, match="worker and task are required"):
        store.start_agent_session(run.run_id, "", "task", lease_token)
    with pytest.raises(StoreError, match="active run is required"):
        store.start_agent_session("missing", "worker", "task", lease_token)
    with pytest.raises(StoreError, match="event kind and source type are required"):
        store.record_agent_session_event(
            run.run_id, lease_token, "tool", "item.completed", None, "ignored"
        )
    with pytest.raises(StoreError, match="active run is required"):
        store.record_agent_session_event(
            "missing", lease_token, "progress", "turn.started", None, "ignored"
        )
    with pytest.raises(StoreError, match="session has not been started"):
        store.record_agent_session_event(
            run.run_id, lease_token, "progress", "turn.started", None, "ignored"
        )
    store.close()


def test_agent_session_events_are_bounded_and_lease_fenced(
    tmp_path: Path,
) -> None:
    store = OrchestratorStore(tmp_path / "state.sqlite3")
    service = Orchestrator(store, FakeProvider(agent_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease_token = run.lease_token or ""
    store.start_agent_session(run.run_id, "worker-1", "bounded task", lease_token)

    for index in range(110):
        store.record_agent_session_event(
            run.run_id, lease_token, "progress", "turn.started", None, str(index)
        )
    session = store.get_agent_session(run.run_id)
    assert session is not None
    assert [event["sequence"] for event in session["events"]] == list(range(11, 111))

    for _ in range(10):
        store.record_agent_session_event(
            run.run_id,
            lease_token,
            "message",
            "agent_message",
            "assistant",
            "é" * 4_000,
        )
    session = store.get_agent_session(run.run_id)
    assert session is not None
    assert [event["sequence"] for event in session["events"]] == list(range(113, 121))
    total_text_bytes = sum(
        len(str(event["text"]).encode("utf-8")) for event in session["events"]
    )
    assert total_text_bytes == 64_000
    assert all(len(str(event["text"])) <= 4_000 for event in session["events"])

    store._connection.execute(
        "UPDATE runs SET lease_expires_at = ? WHERE run_id = ?",
        ("2000-01-01T00:00:00+00:00", run.run_id),
    )
    reclaimed = service.claim("project-1", "owner/api", "worker-2")
    assert reclaimed is not None and reclaimed.run_id == run.run_id
    with pytest.raises(StoreError, match="Invalid or missing run lease token"):
        store.record_agent_session_event(
            run.run_id, lease_token, "progress", "turn.started", None, "stale"
        )
    store.close()


def test_recovered_run_reuses_its_persisted_task_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.FAILURE, failure_context="unused")
    )
    service, store, routing_store, run = service_with_run(executor)
    lease_token = run.lease_token or ""
    contract = TaskContract.inventory(
        "owner/api", 1, "Persisted task", branch="codex/persisted", tracked_file_count=7
    )
    service.advance(run.run_id, Stage.IMPLEMENT, lease_token)
    store.ensure_task_contract(run.run_id, contract, lease_token)
    persisted_run = store.get_run(run.run_id)
    assert persisted_run is not None

    monkeypatch.setattr(
        executor,
        "build_task_contract",
        lambda _run: pytest.fail("recovery rebuilt its persisted task contract"),
    )

    assert service._task_contract_for_run(persisted_run) == contract
    store.close()
    routing_store.close()


def test_worker_manager_rejects_duplicates_and_shutdowns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.SUCCESS, result="unused"), repository
    )
    service, store, routing_store, run = service_with_run(executor)
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)

    class FakeThread:
        def __init__(self, target, args, name, daemon) -> None:
            self.target = target
            self.args = args

        def start(self) -> None:
            return None

        def join(self, timeout=None) -> None:
            return None

    monkeypatch.setattr(agent_module, "Thread", FakeThread)
    manager = AgentWorkerManager(service, executor, workflow_service)
    try:
        with pytest.raises(StoreError, match="active leased"):
            manager.start(replace(run, lease_token=None))
        manager.start(run)
        session = store.get_agent_session(run.run_id)
        assert session is not None
        assert session["session_id"] == run.run_id
        assert session["task"] == executor.task
        executor._session_event_handlers[run.run_id](
            "message", "item.completed", "assistant", "token=private"
        )
        session = store.get_agent_session(run.run_id)
        assert session is not None
        assert session["events"][0]["text"] == "token=[redacted]"
        with pytest.raises(StoreError, match="already active"):
            manager.start(run)
        manager.shutdown()
        stopped = store.get_run(run.run_id)
        assert stopped is not None
        assert stopped.status is RunStatus.FAILED
    finally:
        store.close()
        routing_store.close()
        workflow_store.close()


def test_worker_manager_uses_run_title_when_executor_task_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.FAILURE, failure_context="unused"), repository
    )
    executor.task = ""
    service, store, routing_store, run = service_with_run(executor)
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)

    class IdleThread:
        def __init__(self, target, args, name, daemon) -> None:
            del target, args, name, daemon

        def start(self) -> None:
            return None

        def join(self, timeout=None) -> None:
            return None

    monkeypatch.setattr(agent_module, "Thread", IdleThread)
    manager = AgentWorkerManager(service, executor, workflow_service)
    try:
        manager.start(run)
        session = store.get_agent_session(run.run_id)
        assert session is not None and session["task"] == run.title
    finally:
        manager.shutdown()
        workflow_store.close()
        store.close()
        routing_store.close()


def test_worker_manager_enforces_shared_concurrency_limit(
    tmp_path: Path,
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.SUCCESS, result="unused"), repository
    )
    service, store, routing_store, run = service_with_run(executor)
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)
    manager = AgentWorkerManager(service, executor, workflow_service)
    try:
        assert manager.active_worker_count == 0
        assert manager.has_capacity()
        manager.set_max_concurrent_workers(1)
        manager._threads["occupied"] = Thread(target=lambda: None)
        assert manager.active_worker_count == 1
        assert not manager.has_capacity()
        with pytest.raises(StoreError, match="Maximum concurrent"):
            manager.start(run)
        for invalid_maximum in (True, 0, 1.5):
            with pytest.raises(StoreError, match="positive integer"):
                manager.set_max_concurrent_workers(invalid_maximum)
    finally:
        manager._threads.clear()
        workflow_store.close()
        store.close()
        routing_store.close()


def test_worker_recovery_defers_runs_at_capacity_and_filters_projects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs = [
        SimpleNamespace(
            run_id=run_id,
            project_id=project_id,
            repository=f"owner/{run_id}",
            title=run_id,
        )
        for run_id, project_id in (
            ("run-1", "allowed"),
            ("run-2", "allowed"),
            ("run-3", "outside"),
        )
    ]

    class RecoveryStore:
        def __init__(self) -> None:
            self.runs = runs

        def active_agent_sessions(self) -> tuple[SimpleNamespace, ...]:
            return tuple(self.runs)

        def get_agent_session(self, run_id: str) -> dict[str, str]:
            return {"task": run_id}

    class RecoveryOrchestrator:
        def __init__(self) -> None:
            self.store = RecoveryStore()

        def claim(
            self,
            project_id: str,
            repository: str,
            owner_id: str,
            *,
            expected_run_id: str,
            agent_session: tuple[str, str],
        ) -> SimpleNamespace:
            del project_id, repository, owner_id, agent_session
            return SimpleNamespace(
                run_id=expected_run_id, lease_token=f"lease-{expected_run_id}"
            )

    class RecoveryWorkflow:
        def cleanup_dashboard_run_workspaces(self, run_id: str) -> None:
            del run_id

    orchestrator = RecoveryOrchestrator()
    manager = AgentWorkerManager(
        orchestrator,
        SimpleNamespace(_secret_values=()),
        RecoveryWorkflow(),
    )
    manager.set_max_concurrent_workers(1)
    started: list[str] = []
    capacity_race = True

    def start(run: SimpleNamespace) -> None:
        nonlocal capacity_race
        started.append(run.run_id)
        if run.run_id == "run-1" and capacity_race:
            capacity_race = False
            raise WorkerCapacityError("slot was claimed concurrently")
        manager._threads[run.run_id] = Thread(target=lambda: None)

    monkeypatch.setattr(manager, "start", start)
    assert manager.recover({"allowed"}) == ("run-2",)
    assert started == ["run-1", "run-2"]
    assert [run.run_id for run in orchestrator.store.runs] == [
        "run-1",
        "run-2",
        "run-3",
    ]

    orchestrator.store.runs = [runs[1], runs[0]]
    assert manager.recover({"allowed"}) == ()
    assert len(started) == 2
    manager._threads.clear()
    orchestrator.store.runs = [runs[0]]
    assert manager.recover({"allowed"}) == ("run-1",)
    assert started == ["run-1", "run-2", "run-1"]


def test_worker_start_requires_workflow_service() -> None:
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.SUCCESS, result="unused")
    )
    orchestrator, store, routing_store, run = service_with_run(executor)
    manager = AgentWorkerManager(orchestrator, executor)

    with pytest.raises(StoreError, match="Workflow service is required"):
        manager.start(run)
    with pytest.raises(WorkflowError, match="Workflow service is required"):
        manager._workspace_validator(
            run.run_id,
            WorkspaceLease(
                "lease",
                "dashboard-run:test",
                "codex/test",
                ".",
                LeaseStatus.ACTIVE,
                "created",
                "updated",
                "token",
            ),
        )
    assert manager.recover() == ()

    assert not executor._active_attempts
    store.close()
    routing_store.close()


def test_worker_manager_commit_and_push_rejects_invalid_run_context() -> None:
    repository = Path.cwd().resolve()

    class FakeStore:
        def __init__(self, run) -> None:
            self.run = run

        def get_run(self, run_id: str):
            if self.run is None or self.run.run_id != run_id:
                return None
            return self.run

    class FakeWorkflowService:
        def __init__(self, lease) -> None:
            self.lease = lease
            self.worktrees = SimpleNamespace(repository=repository)

        def cleanup_dashboard_run_workspaces(self):
            return ()

        def workspace_for_run(self, _run_id: str):
            return self.lease

        def commit_and_push(self, *_arguments):
            raise AssertionError("Git delivery must not run for invalid state")

    def run_state(status=RunStatus.ACTIVE, lease_token="run-token"):
        return SimpleNamespace(
            run_id="run-40",
            project_id="project-1",
            repository="owner/api",
            pbi_number=40,
            title="Commit and push",
            status=status,
            lease_token=lease_token,
        )

    def lease_state(status=LeaseStatus.ACTIVE):
        return SimpleNamespace(
            lease_id="lease-40",
            lease_token="workspace-token",
            status=status,
        )

    cases = (
        (run_state(), lease_state(), False, repository, "Workflow service is required"),
        (None, lease_state(), True, repository, "Unknown run"),
        (
            run_state("cancelled"),
            lease_state(),
            True,
            repository,
            "not eligible for Git delivery",
        ),
        (
            run_state(lease_token=None),
            lease_state(),
            True,
            repository,
            "active run lease",
        ),
        (run_state(), None, True, repository, "leased dashboard worktree"),
        (
            run_state(RunStatus.FAILED),
            lease_state(),
            True,
            repository,
            "retained worktree",
        ),
        (
            run_state(),
            lease_state(),
            True,
            repository / "other",
            "same repository",
        ),
    )
    for run, lease, use_service, executor_repository, error in cases:
        store = FakeStore(run)
        service = FakeWorkflowService(lease) if use_service else None
        manager = AgentWorkerManager(
            SimpleNamespace(store=store),
            SimpleNamespace(repository=executor_repository),
            service,
        )
        with pytest.raises(StoreError, match=error):
            manager.commit_and_push("run-40")


def test_worker_manager_revalidates_run_before_git_delivery() -> None:
    repository = Path.cwd().resolve()
    run = SimpleNamespace(
        run_id="run-40",
        project_id="project-1",
        repository="owner/api",
        pbi_number=40,
        title="Commit and push",
        status=RunStatus.ACTIVE,
        lease_token="run-token",
    )

    class FakeStore:
        current = run

        def get_run(self, _run_id: str):
            return self.current

    store = FakeStore()

    class FakeWorkflowService:
        worktrees = SimpleNamespace(repository=repository)

        def cleanup_dashboard_run_workspaces(self):
            return ()

        def workspace_for_run(self, _run_id: str):
            return SimpleNamespace(
                lease_id="lease-40",
                lease_token="workspace-token",
                status=LeaseStatus.ACTIVE,
            )

        def commit_and_push(self, *_arguments):
            store.current = SimpleNamespace(**(vars(run) | {"pbi_number": 41}))
            _arguments[-1]()

    manager = AgentWorkerManager(
        SimpleNamespace(store=store),
        SimpleNamespace(repository=repository),
        FakeWorkflowService(),
    )
    with pytest.raises(WorkflowError, match="Dashboard run lease changed"):
        manager.commit_and_push(run.run_id)


def test_worker_start_rejects_executor_repository_mismatch(tmp_path: Path) -> None:
    workflow_repository = make_git_repository(tmp_path / "workflow-repository")
    executor_repository = make_git_repository(tmp_path / "executor-repository")
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.FAILURE, failure_context="unused"),
        executor_repository,
    )
    orchestrator, store, routing_store, run = service_with_run(executor)
    workflow_service, workflow_store = workflow_service_for(
        tmp_path, workflow_repository
    )
    manager = AgentWorkerManager(orchestrator, executor, workflow_service)

    try:
        with pytest.raises(StoreError, match="must use the same repository"):
            manager.start(run)
        assert workflow_service.workspace_for_run(run.run_id) is None
    finally:
        workflow_store.close()
        store.close()
    routing_store.close()


def test_worker_manager_recovers_expired_session_without_cleaning_other_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    first_executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.FAILURE, failure_context="unused"), repository
    )
    first_executor.task = "configured task " + "x" * 5_000
    orchestrator, store, routing_store, run = service_with_run(first_executor)
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)

    class IdleThread:
        def __init__(self, target, args, name, daemon) -> None:
            del target, args, name, daemon

        def start(self) -> None:
            return None

        def join(self, timeout=None) -> None:
            return None

    monkeypatch.setattr(agent_module, "Thread", IdleThread)
    first_manager = AgentWorkerManager(orchestrator, first_executor, workflow_service)
    first_manager.start(run)
    original_session = store.get_agent_session(run.run_id)
    assert original_session is not None
    original_workspace = workflow_service.workspace_for_run(run.run_id)
    assert original_workspace is not None
    other_workspace = workflow_service.acquire_workspace(
        "dashboard-run:other", "codex/other", tmp_path / "other-worktree"
    )
    second_executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.FAILURE, failure_context="unused"), repository
    )
    second_executor.task = ""
    second_manager = AgentWorkerManager(orchestrator, second_executor, workflow_service)
    try:
        with monkeypatch.context() as context:
            context.setattr(store, "get_agent_session", lambda _run_id: {"task": ""})
            assert second_manager.recover() == ()
        assert Path(original_workspace.worktree_path).exists()
        store._connection.execute(
            "UPDATE runs SET lease_expires_at = ? WHERE run_id = ?",
            ("2000-01-01T00:00:00+00:00", run.run_id),
        )
        assert second_manager.recover() == (run.run_id,)
        recovered_session = store.get_agent_session(run.run_id)
        assert recovered_session is not None
        assert recovered_session["session_id"] == original_session["session_id"]
        assert recovered_session["task"] == original_session["task"]
        assert len(str(recovered_session["task"])) > 4_000
        assert recovered_session["worker_id"] == second_manager._worker_id
        assert second_executor._session_tasks[run.run_id] == original_session["task"]
        assert not Path(original_workspace.worktree_path).exists()
        assert workflow_store.get_lease(other_workspace.lease_id) == other_workspace
    finally:
        workflow_service.cleanup_dashboard_run_workspaces()
        first_executor.release_run(run.run_id)
        second_executor.release_run(run.run_id)
        workflow_store.close()
        store.close()
        routing_store.close()


@pytest.mark.parametrize(
    "failure_point",
    [
        "workspace_cleanup",
        "worker_start",
        "expired_lease_race",
        "rotated_lease_race",
    ],
)
def test_worker_manager_handles_failed_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.FAILURE, failure_context="unused"), repository
    )
    executor.task = "persisted recovery task"
    orchestrator, store, routing_store, run = service_with_run(executor)
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)

    class IdleThread:
        def __init__(self, target, args, name, daemon) -> None:
            del target, args, name, daemon

        def start(self) -> None:
            return None

    monkeypatch.setattr(agent_module, "Thread", IdleThread)
    original_manager = AgentWorkerManager(orchestrator, executor, workflow_service)
    original_manager.start(run)
    lease_token = run.lease_token
    assert lease_token is not None
    store.record_agent_session_event(
        run.run_id, lease_token, "progress", "turn.started", None, "saved progress"
    )
    original_session = store.get_agent_session(run.run_id)
    assert original_session is not None
    store._connection.execute(
        "UPDATE runs SET lease_expires_at = ? WHERE run_id = ?",
        ("2000-01-01T00:00:00+00:00", run.run_id),
    )

    recovery_executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.FAILURE, failure_context="unused"), repository
    )
    recovery_executor.task = ""
    recovery_manager = AgentWorkerManager(
        orchestrator, recovery_executor, workflow_service
    )
    try:
        with monkeypatch.context() as context:
            if failure_point != "worker_start":

                def fail_cleanup(_run_id: str) -> None:
                    raise RuntimeError("workspace cleanup failed")

                context.setattr(
                    workflow_service, "cleanup_dashboard_run_workspaces", fail_cleanup
                )
            else:

                class FailingThread:
                    def __init__(self, target, args, name, daemon) -> None:
                        del target, args, name, daemon

                    def start(self) -> None:
                        raise RuntimeError("thread start failed")

                context.setattr(agent_module, "Thread", FailingThread)

            if failure_point in {"expired_lease_race", "rotated_lease_race"}:
                fail_agent_run = store.fail_agent_run

                def fail_after_lease_change(
                    run_id: str,
                    error: str,
                    claimed_token: str,
                    *,
                    claimable: bool | None = None,
                ) -> RunState:
                    if failure_point == "expired_lease_race":
                        store._connection.execute(
                            "UPDATE runs SET lease_expires_at = ? WHERE run_id = ?",
                            ("2000-01-01T00:00:00+00:00", run_id),
                        )
                    else:
                        store._connection.execute(
                            """
                            UPDATE runs
                            SET owner_id = ?, lease_token = ?, lease_expires_at = ?
                            WHERE run_id = ?
                            """,
                            (
                                "replacement-worker",
                                "replacement-lease-token",
                                "2099-01-01T00:00:00+00:00",
                                run_id,
                            ),
                        )
                    return fail_agent_run(
                        run_id, error, claimed_token, claimable=claimable
                    )

                context.setattr(store, "fail_agent_run", fail_after_lease_change)
            assert recovery_manager.recover() == ()

        result_run = store.get_run(run.run_id)
        assert result_run is not None
        if failure_point == "rotated_lease_race":
            assert result_run.status is RunStatus.ACTIVE
            assert result_run.lease_token == "replacement-lease-token"
            assert result_run.owner_id == "replacement-worker"
        else:
            assert result_run.status is RunStatus.FAILED
            assert result_run.lease_token is None
            assert "Agent recovery failed" in (result_run.last_error or "")
        assert run.run_id not in recovery_manager._threads
        assert run.run_id not in recovery_manager._workspace_leases
        failed_session = store.get_agent_session(run.run_id)
        assert failed_session is not None
        assert failed_session["session_id"] == original_session["session_id"]
        assert failed_session["task"] == original_session["task"]
        assert failed_session["events"] == original_session["events"]
        assert failed_session["worker_id"] == recovery_manager._worker_id
        assert recovery_manager.recover() == ()
    finally:
        workflow_service.cleanup_dashboard_run_workspaces()
        executor.release_run(run.run_id)
        recovery_executor.release_run(run.run_id)
        workflow_store.close()
        store.close()
        routing_store.close()


def test_worker_manager_recovery_rejects_claim_without_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.FAILURE, failure_context="unused"), repository
    )
    orchestrator, store, routing_store, run = service_with_run(executor)
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)
    assert run.lease_token is not None
    store.start_agent_session(run.run_id, "worker-1", "persisted task", run.lease_token)
    manager = AgentWorkerManager(orchestrator, executor, workflow_service)
    monkeypatch.setattr(
        manager,
        "claim",
        lambda *_args, **_kwargs: replace(run, lease_token=None),
    )

    try:
        with pytest.raises(StoreError, match="no lease token"):
            manager.recover()
    finally:
        workflow_store.close()
        store.close()
        routing_store.close()


def test_worker_claim_persists_session_before_start() -> None:
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.FAILURE, failure_context="unused")
    )
    store = OrchestratorStore()
    routing_store = RoutingStore()
    orchestrator = Orchestrator(
        store,
        FakeProvider(agent_snapshot()),
        ModelRouter(routing_store),
        executor,
    )
    orchestrator.synchronize("project-1")
    manager = AgentWorkerManager(orchestrator, executor)

    try:
        with pytest.raises(StoreError, match="agent task"):
            manager.claim("project-1", "owner/api", "dashboard-operator", task=" ")
        assert store.active_runs_for_project("project-1") == ()
        task = "persisted task " + "x" * 5_000
        run = manager.claim("project-1", "owner/api", "dashboard-operator", task=task)
        assert run is not None
        session = store.get_agent_session(run.run_id)
        assert session is not None
        assert session["task"] == task
        assert session["worker_id"] == manager._worker_id
    finally:
        store.close()
        routing_store.close()


def test_workspace_validator_rejects_another_run_lease(tmp_path: Path) -> None:
    repository = make_git_repository(tmp_path / "repository")
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.FAILURE, failure_context="unused"), repository
    )
    orchestrator, store, routing_store, run = service_with_run(executor)
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)
    manager = AgentWorkerManager(orchestrator, executor, workflow_service)
    lease = workflow_service.acquire_workspace(
        "dashboard-run:another-run",
        "codex/another-run",
        tmp_path / "another-run-worktree",
    )

    try:
        validate = manager._workspace_validator(run.run_id, lease)
        with pytest.raises(WorkflowError, match="lease changed"):
            validate()
    finally:
        workflow_service.discard_workspace(lease.lease_id, "test complete")
        workflow_store.close()
        store.close()
        routing_store.close()


def test_worker_manager_rolls_back_when_thread_start_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.SUCCESS, result="unused"), repository
    )
    service, store, routing_store, run = service_with_run(executor)
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)

    class FailingThread:
        def __init__(self, target, args, name, daemon) -> None:
            del target, args, name, daemon

        def start(self) -> None:
            raise RuntimeError("thread start failed")

    monkeypatch.setattr(agent_module, "Thread", FailingThread)
    manager = AgentWorkerManager(service, executor, workflow_service)
    try:
        with pytest.raises(RuntimeError, match="thread start failed"):
            manager.start(run)
        assert run.run_id not in manager._threads
        assert run.run_id not in executor._active_attempts
        lease = workflow_service.workspace_for_run(run.run_id)
        assert lease is not None and lease.status is LeaseStatus.RELEASED
        assert not Path(lease.worktree_path).exists()
    finally:
        store.close()
        routing_store.close()
        workflow_store.close()


def test_worker_start_reports_workspace_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.FAILURE, failure_context="unused"), repository
    )
    orchestrator, store, routing_store, run = service_with_run(executor)
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)
    manager = AgentWorkerManager(orchestrator, executor, workflow_service)

    def fail_repository_files() -> tuple[Path, ...]:
        raise RuntimeError("workspace validation failed")

    original_discard = workflow_service.discard_workspace

    def fail_cleanup(_lease_id: str, _reason: str):
        raise WorkflowError("workspace cleanup unavailable")

    monkeypatch.setattr(executor, "_repository_files", fail_repository_files)
    monkeypatch.setattr(workflow_service, "discard_workspace", fail_cleanup)
    try:
        with pytest.raises(StoreError, match="workspace cleanup failed"):
            manager.start(run)
    finally:
        monkeypatch.setattr(workflow_service, "discard_workspace", original_discard)
        lease = workflow_service.workspace_for_run(run.run_id)
        if lease is not None and lease.status is not LeaseStatus.RELEASED:
            original_discard(lease.lease_id, "test cleanup")
        workflow_store.close()
        store.close()
        routing_store.close()


def test_worker_manager_shutdown_attempts_all_cancellations_after_failure() -> None:
    class FailingExecutor:
        def __init__(self) -> None:
            self.cancelled: list[str] = []

        def cancel(self, run_id: str) -> None:
            self.cancelled.append(run_id)
            if run_id == "run-1":
                raise RuntimeError("taskkill failed")

    class FakeThread:
        def __init__(self) -> None:
            self.joins: list[float | None] = []

        def join(self, timeout=None) -> None:
            self.joins.append(timeout)

        def is_alive(self) -> bool:
            return False

    executor = FailingExecutor()
    manager = AgentWorkerManager(SimpleNamespace(), executor)
    threads = {run_id: FakeThread() for run_id in ("run-1", "run-2")}
    manager._threads.update(threads)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="taskkill failed"):
        manager.shutdown()

    assert executor.cancelled == ["run-1", "run-2"]
    assert all(thread.joins == [5] for thread in threads.values())


def test_worker_manager_shutdown_preserves_workspace_when_worker_stays_alive(
    tmp_path: Path,
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.SUCCESS, result="unused"), repository
    )
    service, store, routing_store, run = service_with_run(executor)
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)

    manager = AgentWorkerManager(service, executor, workflow_service)
    lease = workflow_service.acquire_workspace(
        f"dashboard-run:{run.run_id}",
        "codex/stuck-worker",
        tmp_path / "stuck-worker-worktree",
    )
    output = Path(lease.worktree_path) / "worker-output.txt"
    output.write_text("keep this output\n", encoding="utf-8")

    class StuckThread:
        def __init__(self) -> None:
            self.joins: list[float | None] = []

        def join(self, timeout=None) -> None:
            self.joins.append(timeout)

        def is_alive(self) -> bool:
            return True

    thread = StuckThread()
    manager._threads[run.run_id] = thread  # type: ignore[assignment]
    manager._workspace_leases[run.run_id] = lease
    try:
        with pytest.raises(StoreError, match="did not stop before shutdown"):
            manager.shutdown()
        assert thread.joins == [5, 1]
        still_active = store.get_run(run.run_id)
        assert still_active is not None
        assert still_active.status is RunStatus.ACTIVE
        current_lease = workflow_service.workspace_for_run(run.run_id)
        assert current_lease is not None
        assert current_lease.status is LeaseStatus.ACTIVE
        assert output.read_text(encoding="utf-8") == "keep this output\n"
    finally:
        remaining = workflow_service.workspace_for_run(run.run_id)
        if remaining is not None and remaining.status is not LeaseStatus.RELEASED:
            workflow_service.discard_workspace(remaining.lease_id, "test cleanup")
        store.close()
        routing_store.close()
        workflow_store.close()


def test_worker_manager_persists_model_failure(tmp_path: Path) -> None:
    repository = make_git_repository(tmp_path / "repository")
    executor = ImmediateExecutor(
        ModelExecution(
            AttemptOutcome.FAILURE,
            failure_context="token=worker-secret",
        ),
        repository,
    )
    service, store, routing_store, run = service_with_run(executor)
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)
    manager = AgentWorkerManager(service, executor, workflow_service)
    try:
        manager.start(run)
        deadline = time.monotonic() + 3
        current = store.get_run(run.run_id)
        while (
            current is not None
            and current.status is RunStatus.ACTIVE
            or run.run_id in manager._threads
        ):
            if time.monotonic() >= deadline:
                raise AssertionError("The worker did not finish")
            time.sleep(0.01)
            current = store.get_run(run.run_id)
        assert current is not None
        assert current.status is RunStatus.FAILED
        assert "token=[redacted]" in (current.last_error or "")
        lease = workflow_service.workspace_for_run(run.run_id)
        assert lease is not None and lease.status is LeaseStatus.RELEASED
        assert not Path(lease.worktree_path).exists()
    finally:
        manager.shutdown()
        store.close()
        routing_store.close()
        workflow_store.close()


def test_worker_fails_closed_when_workspace_heartbeat_is_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.FAILURE, failure_context="unused"), repository
    )
    orchestrator, state_store, routing_store, run = service_with_run(executor)
    workflow_store = WorkflowStore(tmp_path / "workflow.db", lease_ttl_seconds=1)
    workflow_service = WorkflowService(
        workflow_store,
        repository,
        Constitution.load(Path(__file__).parents[1] / "constitution.json"),
        [PassingWorkflowCheck()],
    )
    renewal_attempted = Event()

    def fail_renewal(_lease_id: str, _lease_token: str | None) -> WorkspaceLease:
        renewal_attempted.set()
        raise sqlite3.OperationalError("database is locked")

    def wait_for_renewal(_run_id: str, _lease_token: str):
        assert renewal_attempted.wait(3)
        return SimpleNamespace(
            state=SimpleNamespace(status=RoutingStatus.RESOLVED, required_action=None),
            attempt=SimpleNamespace(outcome=AttemptOutcome.FAILURE, failure_context=""),
            decision=SimpleNamespace(failure_context=""),
            execution_result=None,
        )

    monkeypatch.setattr(workflow_store, "renew_lease", fail_renewal)
    monkeypatch.setattr(orchestrator, "run_implementation_attempt", wait_for_renewal)
    manager = AgentWorkerManager(orchestrator, executor, workflow_service)
    try:
        manager.start(run)
        deadline = time.monotonic() + 5
        while run.run_id in manager._threads:
            if time.monotonic() >= deadline:
                raise AssertionError("The worker did not stop after lease loss")
            time.sleep(0.01)

        failed = state_store.get_run(run.run_id)
        assert failed is not None and failed.status is RunStatus.FAILED
        assert "workspace lease was lost" in (failed.last_error or "")
        lease = workflow_service.workspace_for_run(run.run_id)
        assert lease is not None and lease.status is LeaseStatus.RELEASED
        assert not Path(lease.worktree_path).exists()
    finally:
        manager.shutdown()
        workflow_store.close()
        state_store.close()
        routing_store.close()


def test_worker_fails_when_workspace_heartbeat_thread_stays_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.FAILURE, failure_context="unused"), repository
    )
    orchestrator, state_store, routing_store, run = service_with_run(executor)
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)
    manager = AgentWorkerManager(orchestrator, executor, workflow_service)
    lease = workflow_service.acquire_workspace(
        f"dashboard-run:{run.run_id}",
        "codex/stuck-heartbeat",
        tmp_path / "stuck-heartbeat-worktree",
    )
    manager._workspace_leases[run.run_id] = lease
    manager._workspace_validators[run.run_id] = lambda: None
    heartbeat_threads = []

    class StuckThread:
        def __init__(self, target, name, daemon) -> None:
            del target, name, daemon
            self.joins: list[float | None] = []
            heartbeat_threads.append(self)

        def start(self) -> None:
            return None

        def join(self, timeout: float | None = None) -> None:
            self.joins.append(timeout)

        def is_alive(self) -> bool:
            return True

    monkeypatch.setattr(agent_module, "Thread", StuckThread)
    try:
        manager._run(run.run_id, run.lease_token or "")

        failed = state_store.get_run(run.run_id)
        assert failed is not None and failed.status is RunStatus.FAILED
        assert "heartbeat did not stop" in (failed.last_error or "")
        assert len(heartbeat_threads) == 1
        assert len(heartbeat_threads[0].joins) == 2
        released = workflow_service.workspace_for_run(run.run_id)
        assert released is not None and released.status is LeaseStatus.RELEASED
    finally:
        manager.shutdown()
        workflow_store.close()
        state_store.close()
        routing_store.close()


def test_worker_reports_workspace_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.FAILURE, failure_context="unused"), repository
    )
    orchestrator, state_store, routing_store, run = service_with_run(executor)
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)
    manager = AgentWorkerManager(orchestrator, executor, workflow_service)
    lease = workflow_service.acquire_workspace(
        f"dashboard-run:{run.run_id}",
        "codex/cleanup-failure",
        tmp_path / "cleanup-failure-worktree",
    )
    manager._workspace_leases[run.run_id] = lease
    manager._workspace_validators[run.run_id] = lambda: None
    original_discard = workflow_service.discard_workspace

    def fail_discard(_lease_id: str, _reason: str):
        raise WorkflowError("cleanup unavailable")

    monkeypatch.setattr(workflow_service, "discard_workspace", fail_discard)
    try:
        with pytest.raises(StoreError, match="Worker workspace cleanup failed"):
            manager._run(run.run_id, run.lease_token or "")
        failed = state_store.get_run(run.run_id)
        assert failed is not None and failed.status is RunStatus.FAILED
    finally:
        monkeypatch.setattr(workflow_service, "discard_workspace", original_discard)
        remaining = workflow_service.workspace_for_run(run.run_id)
        if remaining is not None and remaining.status is not LeaseStatus.RELEASED:
            original_discard(remaining.lease_id, "test cleanup")
        manager.shutdown()
        workflow_store.close()
        state_store.close()
        routing_store.close()


def test_dashboard_worker_writes_only_in_retained_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    remote = configure_test_remote(repository, tmp_path)
    source_head = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    script = tmp_path / "writer.py"
    script.write_text(
        "import json, os, pathlib, subprocess\n"
        "pathlib.Path('worker-output.txt').write_text('leased\\n')\n"
        "branch = subprocess.check_output(['git', 'branch', '--show-current'], "
        "text=True).strip()\n"
        "files = subprocess.check_output(['git', 'ls-files', '-z']).split(b'\\0')\n"
        "evidence = {'repository': 'owner/api', 'branch': branch, "
        "'tracked_file_count': len([item for item in files if item]), "
        "'credentials_absent': not any(name in os.environ for name in "
        "('GITHUB_TOKEN', 'BEEHAIIVE_API_KEY'))}\n"
        "payload = {'outcome': 'pass', 'evidence': evidence, 'artifact_refs': []}\n"
        "print(json.dumps({'type': 'agent_message', 'text': json.dumps(payload)}))\n",
        encoding="utf-8",
    )

    class WorkspaceScriptExecutor(CodexExecModelExecutor):
        def _workspace_command_for_execution(
            self, prompt: str, model: str, worktree: Path
        ) -> list[str]:
            return [sys.executable, str(script), "--cd", str(worktree), prompt]

    executor = WorkspaceScriptExecutor(
        repository, executable=sys.executable, repository_name="owner/api"
    )
    orchestrator, state_store, routing_store, run = service_with_run(executor)

    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)
    monkeypatch.setenv("GITHUB_TOKEN", "fixture-secret")
    monkeypatch.setenv("BEEHAIIVE_API_KEY", "fixture-api-key")
    provider = orchestrator.provider
    original_create_handoff = provider.create_handoff
    handoff_run_states: list[RunStatus | None] = []

    def record_handoff_state(request):
        current_run = state_store.get_run(request.run_id)
        handoff_run_states.append(None if current_run is None else current_run.status)
        return original_create_handoff(request)

    monkeypatch.setattr(provider, "create_handoff", record_handoff_state)
    manager = AgentWorkerManager(orchestrator, executor, workflow_service)
    try:
        manager.start(run)
        deadline = time.monotonic() + 5
        current = state_store.get_run(run.run_id)
        while current is not None and current.status is RunStatus.ACTIVE:
            if time.monotonic() >= deadline:
                raise AssertionError("The dashboard worker did not finish")
            time.sleep(0.01)
            current = state_store.get_run(run.run_id)

        assert current is not None and current.status is RunStatus.COMPLETED
        assert current.stage.value == "pull_request"
        assert "Git delivery: pushed" in (current.last_result or "")
        assert handoff_run_states == [RunStatus.ACTIVE]
        lease = workflow_service.workspace_for_run(run.run_id)
        assert lease is not None
        assert lease.status is LeaseStatus.RETAINED
        worktree = Path(lease.worktree_path)
        assert (worktree / "worker-output.txt").read_text(
            encoding="utf-8"
        ) == "leased\n"
        assert not (repository / "worker-output.txt").exists()
        source_status = subprocess.run(
            ("git", "status", "--porcelain"),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )
        assert source_status.stdout == ""
        assert lease.lease_id and lease.lease_token and lease.branch
        assert executor._workspace_leases == {}
        delivered_sha = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        remote_head = subprocess.run(
            (
                "git",
                "ls-remote",
                "--exit-code",
                str(remote),
                f"refs/heads/{lease.branch}",
            ),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()[0]
        assert delivered_sha == remote_head
        assert f"Pushed head: {remote_head}" in (current.last_result or "")
        assert '"outcome":"pass"' in (current.last_result or "")
        handoff_record = state_store._connection.execute(
            """
            SELECT branch, pull_request_url, pull_request_number
            FROM handoffs WHERE run_id = ?
            """,
            (run.run_id,),
        ).fetchone()
        assert handoff_record is not None
        assert handoff_record["branch"] == lease.branch
        assert handoff_record["pull_request_url"].endswith("/pull/1")
        assert handoff_record["pull_request_number"] == 1
        handoff_evidence = state_store._connection.execute(
            """
            SELECT handoff_head_sha, handoff_verification_evidence
            FROM pbis WHERE project_id = ? AND repository_name = ? AND number = ?
            """,
            (run.project_id, run.repository, run.pbi_number),
        ).fetchone()
        assert handoff_evidence is not None
        assert handoff_evidence["handoff_head_sha"] == remote_head
        assert '"outcome":"pass"' in handoff_evidence["handoff_verification_evidence"]
        assert (
            subprocess.run(
                ("git", "status", "--porcelain"),
                cwd=worktree,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            == ""
        )
        assert (
            subprocess.run(
                ("git", "rev-parse", "HEAD"),
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            == source_head
        )
        workflow_service.release_workspace(lease.lease_id)
        assert not worktree.exists()
    finally:
        manager.shutdown()
        workflow_store.close()
        state_store.close()
        routing_store.close()


def test_dashboard_worker_shutdown_kills_child_and_preserves_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    script = tmp_path / "slow_writer.py"
    script.write_text(
        "import pathlib, time\n"
        "pathlib.Path('worker-output.txt').write_text('started\\n')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )

    class SlowWorkspaceExecutor(CodexExecModelExecutor):
        def _workspace_command_for_execution(
            self, prompt: str, model: str, worktree: Path
        ) -> list[str]:
            return [sys.executable, str(script), "--cd", str(worktree), prompt]

    executor = SlowWorkspaceExecutor(
        repository, executable=sys.executable, repository_name="owner/api"
    )
    orchestrator, state_store, routing_store, run = service_with_run(executor)
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)
    manager = AgentWorkerManager(orchestrator, executor, workflow_service)
    try:
        manager.start(run)
        deadline = time.monotonic() + 5
        lease = workflow_service.workspace_for_run(run.run_id)
        while (
            lease is not None
            and not Path(lease.worktree_path, "worker-output.txt").exists()
        ):
            if time.monotonic() >= deadline:
                raise AssertionError("The child did not write inside its worktree")
            time.sleep(0.01)
            lease = workflow_service.workspace_for_run(run.run_id)
        assert lease is not None

        def fail_clean(_worktree: Path) -> bool:
            raise WorkflowError("Git status unavailable")

        def fail_retain(_lease_id: str, _lease_token: str | None) -> None:
            raise WorkflowError("Lease changed")

        monkeypatch.setattr(workflow_service.worktrees, "clean", fail_clean)
        monkeypatch.setattr(workflow_service, "retain_workspace", fail_retain)

        manager.shutdown()

        stopped = state_store.get_run(run.run_id)
        assert stopped is not None and stopped.status is RunStatus.FAILED
        preserved = workflow_service.workspace_for_run(run.run_id)
        assert preserved is not None and preserved.status is LeaseStatus.STOPPED
        assert (
            Path(lease.worktree_path, "worker-output.txt").read_text(encoding="utf-8")
            == "started\n"
        )
        assert not executor._processes
        workflow_service.discard_workspace(lease.lease_id, "test cleanup")
    finally:
        manager.shutdown()
        workflow_store.close()
        state_store.close()
        routing_store.close()


def test_worker_manager_records_unexpected_worker_exception() -> None:
    run = RunState(
        "run-1",
        "project-1",
        "owner/api",
        1,
        "Demo PBI",
        Stage.IMPLEMENT,
        RunStatus.ACTIVE,
        1,
        owner_id="worker-1",
        lease_token="lease-1",
    )

    class StubStore:
        def __init__(self) -> None:
            self.failure: tuple[str, str, str] | None = None

        def get_run(self, run_id: str) -> RunState:
            assert run_id == run.run_id
            return run

        def fail_agent_run(self, run_id: str, error: str, lease_token: str) -> None:
            self.failure = (run_id, error, lease_token)

    class FailingOrchestrator:
        def __init__(self) -> None:
            self.store = StubStore()

        def advance(self, run_id: str, target: Stage, lease_token: str) -> None:
            raise RuntimeError("worker exploded")

    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.SUCCESS, result="unused")
    )
    orchestrator = FailingOrchestrator()
    manager = AgentWorkerManager(orchestrator, executor)
    manager._run(run.run_id, run.lease_token or "")

    assert orchestrator.store.failure == (
        run.run_id,
        "Agent worker failed: worker exploded",
        "lease-1",
    )


def test_worker_manager_persists_human_handoff_without_claimability() -> None:
    run = RunState(
        "handoff-run",
        "project-1",
        "owner/api",
        1,
        "Demo PBI",
        Stage.IMPLEMENT,
        RunStatus.ACTIVE,
        1,
        owner_id="worker-1",
        lease_token="lease-1",
    )

    class HandoffStore:
        def __init__(self) -> None:
            self.failure: tuple[str, str, str, bool | None] | None = None

        def fail_agent_run(
            self,
            run_id: str,
            error: str,
            lease_token: str,
            *,
            claimable: bool | None = None,
        ) -> None:
            self.failure = (run_id, error, lease_token, claimable)

    class HandoffOrchestrator:
        def __init__(self) -> None:
            self.store = HandoffStore()

        def advance(self, run_id: str, target: Stage, lease_token: str) -> None:
            del run_id, target, lease_token

        def run_implementation_attempt(self, run_id: str, lease_token: str):
            del run_id, lease_token
            return SimpleNamespace(
                state=SimpleNamespace(
                    status=RoutingStatus.HUMAN_HANDOFF,
                    required_action="Human approval is required",
                ),
                attempt=None,
                decision=SimpleNamespace(failure_context=""),
                execution_result=None,
            )

    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.SUCCESS, result="unused")
    )
    orchestrator = HandoffOrchestrator()
    manager = AgentWorkerManager(orchestrator, executor)
    manager._run(run.run_id, run.lease_token or "")

    assert orchestrator.store.failure == (
        run.run_id,
        "Human approval is required",
        "lease-1",
        False,
    )


def test_worker_manager_reopens_routing_when_handoff_persistence_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    run = RunState(
        "persist-run",
        "project-1",
        "owner/api",
        1,
        "Demo PBI",
        Stage.IMPLEMENT,
        RunStatus.ACTIVE,
        1,
        owner_id="worker-1",
        lease_token="lease-1",
    )

    class PersistenceStore:
        def __init__(self) -> None:
            self.failure: tuple[str, str, str] | None = None

        def get_run(self, run_id: str) -> RunState:
            assert run_id == run.run_id
            return run

        def fail_agent_run(self, run_id: str, error: str, lease_token: str) -> None:
            self.failure = (run_id, error, lease_token)

    class PersistenceOrchestrator:
        def __init__(self) -> None:
            self.store = PersistenceStore()
            self.recovery: tuple[str, str] | None = None

        def advance(self, run_id: str, target: Stage, lease_token: str) -> None:
            del run_id, target, lease_token

        def run_implementation_attempt(self, run_id: str, lease_token: str):
            del run_id, lease_token
            (worktree / "worker-output.txt").write_text(
                "successful work\n", encoding="utf-8"
            )
            return SimpleNamespace(
                state=SimpleNamespace(
                    status=RoutingStatus.RESOLVED,
                    required_action=None,
                ),
                attempt=SimpleNamespace(outcome=AttemptOutcome.SUCCESS),
                decision=SimpleNamespace(failure_context=""),
                execution_result="completed result",
                task_result=TaskResult(TaskOutcome.PASS, {}),
            )

        def handoff(self, *args, **kwargs) -> None:
            del args, kwargs
            raise RuntimeError("database unavailable")

        def recover_routing_problem(self, run_id: str, reason: str) -> None:
            self.recovery = (run_id, reason)

    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.SUCCESS, result="unused"), repository
    )
    orchestrator = PersistenceOrchestrator()
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)
    manager = AgentWorkerManager(orchestrator, executor, workflow_service)
    lease = workflow_service.acquire_workspace(
        f"dashboard-run:{run.run_id}",
        "codex/persist-run",
        tmp_path / "persist-run-worktree",
    )
    worktree = Path(lease.worktree_path)
    manager._workspace_leases[run.run_id] = lease
    monkeypatch.setattr(
        manager,
        "commit_and_push",
        lambda _run_id: GitDeliveryResult(
            GitDeliveryStatus.PUSHED,
            lease.lease_id,
            lease.branch,
            "a" * 40,
            "push verified",
        ),
    )

    try:
        manager._run(run.run_id, run.lease_token or "")

        assert orchestrator.recovery is not None
        assert orchestrator.recovery[0] == run.run_id
        assert "database unavailable" in orchestrator.recovery[1]
        assert orchestrator.store.failure == (
            run.run_id,
            "Agent worker failed: database unavailable",
            "lease-1",
        )
        retained = workflow_service.workspace_for_run(run.run_id)
        assert retained is not None
        assert retained.status is LeaseStatus.RETAINED
        assert (worktree / "worker-output.txt").read_text(encoding="utf-8") == (
            "successful work\n"
        )
    finally:
        remaining = workflow_service.workspace_for_run(run.run_id)
        if remaining is not None and remaining.status is not LeaseStatus.RELEASED:
            workflow_service.discard_workspace(remaining.lease_id, "test cleanup")
        workflow_store.close()


def test_worker_manager_recovers_when_failure_lease_is_lost() -> None:
    run = RunState(
        "lease-loss-run",
        "project-1",
        "owner/api",
        1,
        "Demo PBI",
        Stage.IMPLEMENT,
        RunStatus.ACTIVE,
        1,
        owner_id="worker-1",
        lease_token="lease-1",
    )

    class LeaseLossStore:
        def __init__(self) -> None:
            self.recovery: tuple[str, str, str] | None = None

        def get_run(self, run_id: str) -> RunState:
            assert run_id == run.run_id
            return run

        def fail_agent_run(self, run_id: str, error: str, lease_token: str) -> None:
            del run_id, error, lease_token
            raise StoreError("lease changed")

        def fail_agent_run_after_lease_loss(
            self, run_id: str, error: str, *, expected_lease_token: str
        ) -> None:
            self.recovery = (run_id, error, expected_lease_token)

    class LeaseLossOrchestrator:
        def __init__(self) -> None:
            self.store = LeaseLossStore()

        def advance(self, run_id: str, target: Stage, lease_token: str) -> None:
            del run_id, target, lease_token
            raise RuntimeError("worker exploded")

    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.SUCCESS, result="unused")
    )
    orchestrator = LeaseLossOrchestrator()
    manager = AgentWorkerManager(orchestrator, executor)
    manager._run(run.run_id, run.lease_token or "")

    assert orchestrator.store.recovery == (
        run.run_id,
        "Agent worker failed: worker exploded",
        "lease-1",
    )


def test_demo_review_adapters_cover_all_required_concerns() -> None:
    provider, readers = demo_review_adapters()
    target = provider.get_pull_request("demo-pr")
    assert target.head_sha == "demo-head"
    assert set(readers) == set(REQUIRED_CONCERNS)
    for concern in REQUIRED_CONCERNS:
        assert readers[concern].review(target).status is ReaderStatus.PASS


def test_agent_run_completion_and_failure_persist_bounded_state() -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, FakeProvider(agent_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease_token = run.lease_token or ""
    try:
        with pytest.raises(StoreError, match="result is required"):
            store.complete_agent_run(run.run_id, " ", lease_token)
        with pytest.raises(StoreError, match="Unknown run"):
            store.complete_agent_run("missing", "result", lease_token)
        with pytest.raises(StoreError, match="active implementation"):
            store.complete_agent_run(run.run_id, "result", lease_token)

        implementation = store.advance(run.run_id, Stage.IMPLEMENT, lease_token)
        contract = TaskContract.inventory("owner/api", run.pbi_number, run.title)
        store.ensure_task_contract(
            run.run_id, contract, implementation.lease_token or ""
        )
        store.record_task_result(
            run.run_id,
            TaskResult(TaskOutcome.PASS, {}),
            implementation.lease_token or "",
        )
        completed = store.complete_agent_run(
            run.run_id,
            "x" * 5_000,
            implementation.lease_token or "",
        )
        assert completed.status is RunStatus.COMPLETED
        assert completed.last_result == "x" * 4_000
        assert completed.lease_token is None
        assert store.complete_agent_run(run.run_id, "ignored", "stale") == completed
        with pytest.raises(StoreError, match="completed run"):
            store.fail_agent_run(run.run_id, "late failure", "stale")
    finally:
        store.close()

    failure_store = OrchestratorStore()
    failure_service = Orchestrator(failure_store, FakeProvider(agent_snapshot()))
    failure_service.synchronize("project-1")
    failed_run = failure_service.claim("project-1", "owner/api", "worker-1")
    assert failed_run is not None
    failure_lease = failed_run.lease_token or ""
    try:
        with pytest.raises(StoreError, match="failure reason"):
            failure_store.fail_agent_run(failed_run.run_id, " ", failure_lease)
        with pytest.raises(StoreError, match="Unknown run"):
            failure_store.fail_agent_run("missing", "error", failure_lease)
        failed = failure_store.fail_agent_run(
            failed_run.run_id,
            "e" * 5_000,
            failure_lease,
        )
        assert failed.status is RunStatus.FAILED
        assert failed.last_error == "e" * 4_000
        assert failed.lease_token is None
        assert (
            failure_store.fail_agent_run(failed_run.run_id, "ignored", "stale")
            == failed
        )
        state = failure_store.project_state("project-1")
        pbi = state["repositories"][0]["pbis"][0]
        assert pbi["claimable"] is True
    finally:
        failure_store.close()


def test_lease_loss_failure_recovery_clears_active_worker_state() -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, FakeProvider(agent_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease = run.lease_token or ""
    implementation = store.advance(run.run_id, Stage.IMPLEMENT, lease)
    try:
        with pytest.raises(StoreError, match="Unknown run"):
            store.set_run_claimable("missing", False)
        with pytest.raises(StoreError, match="failure reason"):
            store.fail_agent_run_after_lease_loss(
                run.run_id, " ", expected_lease_token=lease
            )
        with pytest.raises(StoreError, match="lease token"):
            store.fail_agent_run_after_lease_loss(
                run.run_id, "worker failed", expected_lease_token=""
            )
        with pytest.raises(StoreError, match="Unknown run"):
            store.fail_agent_run_after_lease_loss(
                "missing", "worker failed", expected_lease_token=lease
            )
        failed = store.fail_agent_run_after_lease_loss(
            implementation.run_id,
            "lease expired while executing",
            expected_lease_token=lease,
        )
        assert failed.status is RunStatus.FAILED
        assert failed.lease_token is None
        assert (
            store.fail_agent_run_after_lease_loss(
                run.run_id, "ignored", expected_lease_token=lease
            )
            == failed
        )
    finally:
        store.close()
