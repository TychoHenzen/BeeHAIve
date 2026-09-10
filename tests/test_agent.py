import json
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from threading import Thread

import pytest
from conftest import FakeProvider

import beehaiive.agent as agent_module
from beehaiive.agent import AgentWorkerManager, CodexExecModelExecutor
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
    RoutingStore,
)
from beehaiive.storage import OrchestratorStore, StoreError


class ScriptExecutor(CodexExecModelExecutor):
    def __init__(
        self, repository: Path, script: Path, timeout_seconds: float = 5
    ) -> None:
        super().__init__(
            repository,
            executable=sys.executable,
            timeout_seconds=timeout_seconds,
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


class ImmediateExecutor(CodexExecModelExecutor):
    def __init__(self, result: ModelExecution) -> None:
        super().__init__(Path.cwd())
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


def test_executor_honors_cancellation_before_and_during_launch(
    tmp_path: Path,
) -> None:
    router = ModelRouter(RoutingStore())
    executor = ScriptExecutor(tmp_path, tmp_path / "unused.py")
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

    class FinishedProcess:
        pid = 1

        def poll(self):
            return 0

    CodexExecModelExecutor._terminate_process(FinishedProcess())

    class Process:
        pid = 2

        def __init__(self, timeout_on_first_wait: bool = False) -> None:
            self.terminated = False
            self.killed = False
            self.waits = 0
            self.timeout_on_first_wait = timeout_on_first_wait

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            self.waits += 1
            if self.timeout_on_first_wait and self.waits == 1:
                raise subprocess.TimeoutExpired("process", timeout)

    kill_groups: list[tuple[int, int]] = []
    monkeypatch.setattr(agent_module.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(
        agent_module.os,
        "killpg",
        lambda group, sig: kill_groups.append((group, sig)),
        raising=False,
    )
    clean = Process()
    CodexExecModelExecutor._terminate_process(clean)
    assert kill_groups
    assert not clean.terminated

    def raise_kill_group(group, sig):
        raise OSError("already gone")

    monkeypatch.setattr(agent_module.os, "killpg", raise_kill_group, raising=False)
    fallback = Process()
    CodexExecModelExecutor._terminate_process(fallback)
    assert fallback.terminated

    timed_kill_groups = 0

    def terminate_then_fallback(group, sig):
        nonlocal timed_kill_groups
        timed_kill_groups += 1
        if timed_kill_groups == 2:
            raise OSError("already gone")

    monkeypatch.setattr(
        agent_module.os,
        "killpg",
        terminate_then_fallback,
        raising=False,
    )
    monkeypatch.setattr(agent_module.signal, "SIGKILL", 9, raising=False)
    timed = Process(timeout_on_first_wait=True)
    CodexExecModelExecutor._terminate_process(timed)
    assert timed.killed
    assert timed.waits == 2

    monkeypatch.setattr(agent_module.os, "name", "nt")
    monkeypatch.setattr(agent_module.subprocess, "run", lambda *args, **kwargs: None)
    windows_timed = Process(timeout_on_first_wait=True)
    CodexExecModelExecutor._terminate_process(windows_timed)
    assert windows_timed.killed


def test_executor_text_parser_rejects_unsupported_values() -> None:
    assert agent_module._text_value(3) == ""


def test_worker_manager_rejects_duplicates_and_shutdowns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.SUCCESS, result="unused")
    )
    service, store, routing_store, run = service_with_run(executor)

    class FakeThread:
        def __init__(self, target, args, name, daemon) -> None:
            self.target = target
            self.args = args

        def start(self) -> None:
            return None

        def join(self, timeout=None) -> None:
            return None

    monkeypatch.setattr(agent_module, "Thread", FakeThread)
    manager = AgentWorkerManager(service, executor)
    try:
        with pytest.raises(StoreError, match="active leased"):
            manager.start(replace(run, lease_token=None))
        manager.start(run)
        with pytest.raises(StoreError, match="already active"):
            manager.start(run)
        manager.shutdown()
        stopped = store.get_run(run.run_id)
        assert stopped is not None
        assert stopped.status is RunStatus.FAILED
    finally:
        store.close()
        routing_store.close()


def test_worker_manager_persists_model_failure() -> None:
    executor = ImmediateExecutor(
        ModelExecution(
            AttemptOutcome.FAILURE,
            failure_context="token=worker-secret",
        )
    )
    service, store, routing_store, run = service_with_run(executor)
    manager = AgentWorkerManager(service, executor)
    try:
        manager.start(run)
        deadline = time.monotonic() + 3
        current = store.get_run(run.run_id)
        while current is not None and current.status is RunStatus.ACTIVE:
            if time.monotonic() >= deadline:
                raise AssertionError("The worker did not finish")
            time.sleep(0.01)
            current = store.get_run(run.run_id)
        assert current is not None
        assert current.status is RunStatus.FAILED
        assert "token=[redacted]" in (current.last_error or "")
    finally:
        manager.shutdown()
        store.close()
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
