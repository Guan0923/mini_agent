"""Plan proposal review and implementation handoff workflow."""

from __future__ import annotations

from mini_agent.domain import AssistantMessage, RunHandoff

from .cancellation import cancel_if_requested
from .context import AgentRuntime
from .contracts import InterruptRequest
from .events import RuntimeEvent
from .outcomes import cancel_run, complete_run, fail_run
from .workflows import PlanProposalWorkflow


class PlanModeWorkflow:
    """Record and review one plan without owning run dispatch."""

    def __init__(self, proposal_workflow: PlanProposalWorkflow | None = None) -> None:
        self._proposal = proposal_workflow or PlanProposalWorkflow()

    def run(self, runtime: AgentRuntime) -> None:
        proposal = self._proposal.prepare(runtime)
        if proposal is None:
            return
        if cancel_if_requested(runtime):
            return
        if proposal.plan is None:
            complete_run(runtime, proposal.message, event_kind="response")
            return
        self._review(runtime, proposal.message, proposal.plan)

    @staticmethod
    def _review(runtime: AgentRuntime, message: AssistantMessage, proposal: str) -> None:
        request = InterruptRequest(
            "plan",
            "Choose how to handle this plan.",
            {
                "run_id": runtime.run.run_id,
                "plan": proposal,
            },
        )
        runtime.run.add_event("approval_requested", "Plan implementation decision requested", **request.data)
        publish = runtime.services.publish or (lambda _event: None)
        publish(RuntimeEvent("approval_requested", request.message, request.data))
        if runtime.services.interrupt is None:
            cancel_run(runtime)
            return

        decision = runtime.services.interrupt(request)
        if cancel_if_requested(runtime):
            return
        if decision.choice == "cancel":
            cancel_run(runtime)
            return
        if decision.choice not in {"implement", "implement_clear_session"}:
            fail_run(runtime, f"Invalid Plan Review decision: {decision.choice}.")
            return

        new_session = decision.choice == "implement_clear_session"
        runtime.run.handoff = RunHandoff(
            "agent",
            "Implement the plan",
            new_session=new_session,
        )
        runtime.run.add_event(
            "approval_granted",
            "Plan implementation requested",
            new_session=new_session,
            **request.data,
        )
        publish(RuntimeEvent("approval_granted", request.message, {**request.data, "new_session": new_session}))
        runtime.run.add_event(
            "handoff_created",
            "Agent implementation handoff created",
            task=runtime.run.handoff.task,
            new_session=new_session,
        )
        publish(
            RuntimeEvent(
                "handoff_created",
                runtime.run.handoff.task,
                {
                    "mode": runtime.run.handoff.mode,
                    "new_session": new_session,
                },
            )
        )
        complete_run(runtime, message, final_answer=proposal, event_kind="plan")
