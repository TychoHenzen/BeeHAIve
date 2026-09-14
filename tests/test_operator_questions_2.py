from beehaiive.contracts import TaskOutcome, TaskResult
from beehaiive.models import Stage
from beehaiive.orchestrator import Orchestrator
from beehaiive.routing import (
    AttemptOutcome,
    ModelExecution,
    ModelRouter,
    RoutingStatus,
    RoutingStore,
)
from beehaiive.storage import OrchestratorStore
from tests.support.routing.helpers import build_routing_config
from tests.support.routing.routing_provider import RoutingProvider


class QuestionModel:
    def execute(self, _spec, _decision):
        return ModelExecution(
            AttemptOutcome.SUCCESS,
            task_result=TaskResult(
                TaskOutcome.QUESTION,
                {},
                question="Which branch should be used?",
            ),
        )


def test_question_result_moves_the_run_to_durable_operator_handoff() -> None:
    routing_store = RoutingStore()
    router = ModelRouter(routing_store, build_routing_config())
    store = OrchestratorStore()
    orchestrator = Orchestrator(store, RoutingProvider(), router, QuestionModel())
    orchestrator.synchronize("owner:7")
    run = orchestrator.claim("owner:7", "owner/api", "worker-1")
    assert run is not None
    lease = run.lease_token or ""
    orchestrator.advance(run.run_id, Stage.IMPLEMENT, lease)

    result = orchestrator.run_implementation_attempt(run.run_id, lease)

    assert result.state.status is RoutingStatus.HUMAN_HANDOFF
    waiting = store.get_run(run.run_id)
    question = store.operator_question_for_run(run.run_id)
    assert waiting is not None
    assert waiting.status.value == "awaiting_operator"
    assert waiting.lease_token is None
    assert question is not None
    assert question["run_id"] == run.run_id
    assert question["kind"] == "question"
    assert question["status"] == "pending"
    assert question["question"] == "Which branch should be used?"
    assert (
        store.project_state("owner:7")["repositories"][0]["pbis"][0]["claimable"]
        is False
    )
    store.close()
    routing_store.close()
