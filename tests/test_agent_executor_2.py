import time
from pathlib import Path

from beehaiive.agent import (
    CodexExecModelExecutor,
)
from beehaiive.contracts import TaskContract, TaskOutcome
from beehaiive.routing import (
    AttemptOutcome,
    ModelRouter,
    ModelTier,
    RoutingStore,
)
from tests.support.agent.repair_script_executor import (
    RepairScriptExecutor as RepairScriptExecutor,
)
from tests.support.agent.script_executor import ScriptExecutor as ScriptExecutor


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
