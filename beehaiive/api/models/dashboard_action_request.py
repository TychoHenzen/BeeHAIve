from typing import get_args

from ._common import Annotated, Field
from .dashboard_advance_request import DashboardAdvanceRequest
from .dashboard_answer_question_request import DashboardAnswerQuestionRequest
from .dashboard_approve_request import DashboardApproveRequest
from .dashboard_clarify_request import DashboardClarifyRequest
from .dashboard_commit_push_request import DashboardCommitPushRequest
from .dashboard_retry_request import DashboardRetryRequest
from .dashboard_start_request import DashboardStartRequest
from .dashboard_stop_request import DashboardStopRequest
from .dashboard_synchronize_request import DashboardSynchronizeRequest

__all__ = ["DASHBOARD_ACTIONS", "DashboardActionRequest"]

DashboardActionRequest = Annotated[
    DashboardStartRequest
    | DashboardSynchronizeRequest
    | DashboardAdvanceRequest
    | DashboardRetryRequest
    | DashboardAnswerQuestionRequest
    | DashboardStopRequest
    | DashboardApproveRequest
    | DashboardClarifyRequest
    | DashboardCommitPushRequest,
    Field(discriminator="action"),
]

DASHBOARD_ACTIONS = frozenset(
    value
    for model in get_args(get_args(DashboardActionRequest)[0])
    for value in get_args(model.model_fields["action"].annotation)
    if isinstance(value, str)
)
