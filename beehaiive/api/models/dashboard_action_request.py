from ._common import Annotated, Field
from .dashboard_answer_question_request import DashboardAnswerQuestionRequest
from .dashboard_approve_request import DashboardApproveRequest
from .dashboard_clarify_request import DashboardClarifyRequest
from .dashboard_commit_push_request import DashboardCommitPushRequest
from .dashboard_start_request import DashboardStartRequest
from .dashboard_stop_request import DashboardStopRequest

__all__ = ["DashboardActionRequest"]

DashboardActionRequest = Annotated[
    DashboardStartRequest
    | DashboardAnswerQuestionRequest
    | DashboardStopRequest
    | DashboardApproveRequest
    | DashboardClarifyRequest
    | DashboardCommitPushRequest,
    Field(discriminator="action"),
]
