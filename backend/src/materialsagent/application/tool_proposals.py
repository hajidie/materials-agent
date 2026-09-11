from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from materialsagent.application.tool_registry import ToolRegistry, UnknownToolError
from materialsagent.domain.models.tool_invocation import (
    ProposalOrigin,
    ToolInvocationProposal,
)
from materialsagent.domain.ports.tool_registry import RegisteredTool, RoutingCatalogSnapshot


class ToolProposalError(ValueError):
    error_code = "TOOL_PROPOSAL_INVALID"


class MultipleToolCallsUnsupportedError(ToolProposalError):
    error_code = "MULTIPLE_TOOL_CALLS_UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class ResolvedToolInvocationProposal:
    proposal: ToolInvocationProposal
    registration: RegisteredTool

    @property
    def tool_ref(self):
        return self.registration.ref


def native_tool_call_proposal(
    tool_calls: Sequence[Mapping[str, object]],
    *,
    conversation_id: str,
    source_message_id: str,
    llm_call_id: str,
) -> ToolInvocationProposal | None:
    if len(tool_calls) == 0:
        return None
    if len(tool_calls) != 1:
        raise MultipleToolCallsUnsupportedError(
            "A model response may contain at most one executable Tool call."
        )
    call = tool_calls[0]
    name = call.get("name")
    arguments = call.get("args")
    call_id = call.get("id")
    if type(name) is not str or not isinstance(arguments, Mapping):
        raise ToolProposalError("Native Tool call is invalid.")
    return ToolInvocationProposal(
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        llm_call_id=llm_call_id,
        model_tool_name=name,
        proposed_arguments=arguments,
        origin=ProposalOrigin.NATIVE,
        provider_tool_call_id=call_id if type(call_id) is str else None,
    )


def resolve_tool_proposal(
    registry: ToolRegistry,
    snapshot: RoutingCatalogSnapshot,
    proposal: ToolInvocationProposal,
) -> ResolvedToolInvocationProposal:
    entries = [
        entry
        for entry in snapshot.entries
        if entry.tool_id == proposal.model_tool_name
    ]
    if len(entries) != 1:
        raise ToolProposalError("Model Tool name is not in the Routing Snapshot.")
    try:
        registration = registry.resolve(
            entries[0].tool_id,
            snapshot=snapshot,
        )
    except UnknownToolError:
        raise ToolProposalError("Tool registration changed after routing.") from None
    if registration.ref != entries[0].ref:
        raise ToolProposalError("Tool registration changed after routing.")
    return ResolvedToolInvocationProposal(proposal, registration)
