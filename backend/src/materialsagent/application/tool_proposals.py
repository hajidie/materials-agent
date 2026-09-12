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
