"""Plan proposal review and implementation handoff workflow."""

from __future__ import annotations

from mini_agent.domain import ArtifactMessage, RunHandoff

from .context import AgentRuntime
from .contracts import InterruptRequest
from .events import RuntimeEvent
from .outcomes import cancel_run, complete_run, fail_run
from .workflows import PlanProposalWorkflow


class PlanModeWorkflow:
    """Create, persist, and review one plan without owning run dispatch."""

    def __init__(self, proposal_workflow: PlanProposalWorkflow | None = None) -> None:
        self._proposal = proposal_workflow or PlanProposalWorkflow()

    def run(self, runtime: AgentRuntime) -> None:
        proposal = self._proposal.prepare(runtime)
        if proposal is None:
            return
        try:
            artifact = runtime.services.artifact_store.create_plan(
                runtime.state.session_id,
                runtime.run.run_id,
                1,
                proposal,
            )
        except (OSError, ValueError) as exc:
            fail_run(runtime, f"Unable to persist plan artifact: {exc}")
            return

        self._record_artifact(runtime, artifact)
        self._review(runtime, proposal, artifact)

    @staticmethod
    def _record_artifact(runtime: AgentRuntime, artifact: ArtifactMessage) -> None:
        runtime.state.messages.append(artifact)
        runtime.run.history = runtime.state.messages
        runtime.run.artifact_ids.append(artifact.artifact_id)
        runtime.state.pending_plan_artifact_id = artifact.artifact_id
        runtime.run.add_event(
            "artifact_created",
            "Plan artifact created",
            artifact_id=artifact.artifact_id,
            artifact_path=artifact.relative_path,
            revision=artifact.revision,
        )
        publish = runtime.services.publish or (lambda _event: None)
        publish(
            RuntimeEvent(
                "artifact_created",
                artifact.relative_path or artifact.artifact_id,
                {
                    "artifact_id": artifact.artifact_id,
                    "artifact_path": artifact.relative_path,
                    "revision": artifact.revision,
                },
            )
        )
        runtime.save()

    @staticmethod
    def _review(runtime: AgentRuntime, proposal: str, artifact: ArtifactMessage) -> None:
        request = InterruptRequest(
            "plan",
            "Choose how to handle this plan.",
            {
                "run_id": runtime.run.run_id,
                "plan": proposal,
                "artifact_id": artifact.artifact_id,
                "artifact_path": artifact.relative_path,
                "revision": artifact.revision,
            },
        )
        runtime.run.add_event("approval_requested", "Plan implementation decision requested", **request.data)
        publish = runtime.services.publish or (lambda _event: None)
        publish(RuntimeEvent("approval_requested", request.message, request.data))
        if runtime.services.interrupt is None:
            cancel_run(runtime)
            return

        decision = runtime.services.interrupt(request)
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
            artifact.artifact_id,
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
            artifact_id=artifact.artifact_id,
            task=runtime.run.handoff.task,
            new_session=new_session,
        )
        publish(
            RuntimeEvent(
                "handoff_created",
                runtime.run.handoff.task,
                {
                    "artifact_id": artifact.artifact_id,
                    "mode": runtime.run.handoff.mode,
                    "new_session": new_session,
                },
            )
        )
        complete_run(runtime, artifact)
