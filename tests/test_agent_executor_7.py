from pathlib import Path

import pytest

import beehaiive.agent as agent_module
from beehaiive.agent import (
    AgentWorkerManager,
    CodexExecModelExecutor,
)
from beehaiive.routing import (
    AttemptOutcome,
    ModelExecution,
)
from beehaiive.storage import StoreError
from tests.support.agent.helpers import make_git_repository as make_git_repository
from tests.support.agent.helpers import service_with_run as service_with_run
from tests.support.agent.helpers import workflow_service_for as workflow_service_for
from tests.support.agent.immediate_executor import (
    ImmediateExecutor as ImmediateExecutor,
)


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

    monkeypatch.setattr(
        "beehaiive.agent_parts.codex_process_mixin.Thread", OneStuckReader
    )
    CodexExecModelExecutor._communicate_bounded(OneStreamProcess(), 1)
    assert stream.closed is True


def test_executor_text_parser_rejects_unsupported_values() -> None:
    assert agent_module._text_value(3) == ""


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

    monkeypatch.setattr(
        "beehaiive.agent_parts.worker_capacity_mixin.Thread", IdleThread
    )
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
