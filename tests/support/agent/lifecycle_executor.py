from collections.abc import Mapping

from beehaiive.autonomous import AUTONOMOUS_STEPS, SkillStep


class SuccessfulSkillExecutor:
    def execute(
        self,
        step: SkillStep,
        context: Mapping[str, object],
        handover: Mapping[str, object],
    ) -> Mapping[str, object]:
        try:
            step_index = next(
                index
                for index, candidate in enumerate(AUTONOMOUS_STEPS)
                if candidate.name == step.name
            )
            remaining = AUTONOMOUS_STEPS[step_index + 1 :]
        except StopIteration:
            remaining = ()
        next_step = remaining[0].name if remaining else "complete"
        return {
            "status": "succeeded",
            "summary": f"Test context completed {step.name}.",
            "handover": {
                **dict(handover),
                "completed_skill": step.name,
                "next_skill": next_step,
                "context_project": context.get("project_id"),
            },
        }
