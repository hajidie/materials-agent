"""Explicit failure policy shared by execution and recovery boundaries."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import math

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError
from langchain_core.exceptions import (ModelError, ModelAuthenticationError, ModelPermissionDeniedError,
    ModelNotFoundError, ModelRateLimitError, ModelAPIError, ModelConnectionError, ModelTimeoutError)

from materialsagent.domain.ports.agent import AgentFailure


@dataclass(frozen=True)
class ModelFault:
    category: str
    code: str
    recoverable: bool = False
    auto_retry: bool = False
    not_before: datetime | None = None


def retry_after(headers, instant: datetime) -> datetime | None:
    value = headers.get("retry-after") if headers else None
    if not value:
        return None
    try:
        seconds = float(value)
        if math.isfinite(seconds) and seconds >= 0:
            return instant + timedelta(seconds=seconds)
    except (ValueError, TypeError, OverflowError):
        pass
    try:
        date = parsedate_to_datetime(value)
        date = date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date
        return max(instant, date)
    except (ValueError, TypeError, OverflowError):
        return None


def model_fault(error: BaseException, instant: datetime | None = None) -> ModelFault:
    instant = instant or datetime.now(timezone.utc)
    if isinstance(error, AgentFailure):
        if error.code == "LLM_RECOVERY_WINDOW_EXCEEDED":
            return ModelFault("TRANSIENT", error.code, True)
        if error.code in {"LLM_RESPONSE_INVALID", "INVALID_TOOL_CALL", "PARALLEL_TOOL_CALLS_NOT_ALLOWED",
                          "UNKNOWN_TOOL", "FINAL_ANSWER_TRUNCATED", "FINAL_ANSWER_INVALID"}:
            return ModelFault("PROTOCOL", error.code)
        return ModelFault("INTERNAL", error.code)
    if isinstance(error, (APIConnectionError, APITimeoutError, httpx.TransportError, TimeoutError)):
        return ModelFault("TRANSIENT", "LLM_TEMPORARILY_UNAVAILABLE", True, True)
    if isinstance(error, (APIStatusError, httpx.HTTPStatusError)):
        response = error.response
        status = response.status_code
        body = getattr(error, "body", None)
        detail = body.get("error", body) if isinstance(body, dict) else {}
        provider_code = detail.get("code") if isinstance(detail, dict) else None
        if status in {401, 402, 403, 404} or provider_code in {
            "insufficient_quota", "insufficient_balance", "quota_exceeded", "invalid_api_key", "billing_hard_limit_reached",
        }:
            return ModelFault("CONFIGURATION", "LLM_CONFIGURATION_REQUIRED", True)
        if status in {408, 409, 429, 500, 502, 503, 504}:
            date = retry_after(response.headers, instant)
            return ModelFault("TRANSIENT", "LLM_RETRY_AFTER" if date else "LLM_TEMPORARILY_UNAVAILABLE",
                              True, date is None, date)
        return ModelFault("PROTOCOL", "LLM_REQUEST_REJECTED")
    if isinstance(error, (ModelAuthenticationError, ModelPermissionDeniedError, ModelNotFoundError)):
        return ModelFault("CONFIGURATION", "LLM_CONFIGURATION_REQUIRED", True)
    if isinstance(error, (ModelRateLimitError, ModelAPIError, ModelConnectionError, ModelTimeoutError)) and error.is_retryable:
        date = retry_after(getattr(getattr(error, "response", None), "headers", None), instant)
        return ModelFault("TRANSIENT", "LLM_RETRY_AFTER" if date else "LLM_TEMPORARILY_UNAVAILABLE", True, date is None, date)
    if isinstance(error, ModelError):
        return ModelFault("PROTOCOL", "LLM_REQUEST_REJECTED")
    return ModelFault("INTERNAL", "LLM_CALL_FAILED")


def retry_model_error(error: Exception) -> bool:
    return model_fault(error).auto_retry


def unknown_observation(observation) -> bool:
    return observation is None or observation.status == "OUTCOME_UNKNOWN" or (
        observation.error or {}).get("code") in {"MCP_OUTCOME_UNKNOWN", "RUNTIME_OUTCOME_UNKNOWN", "TOOL_OUTCOME_UNKNOWN"}


def recovery_action(run) -> str:
    if run.status != "INTERRUPTED":
        return "NONE"
    if run.pending_execution and run.pending_execution.dispatched:
        return "RECONCILE"
    if (run.active_seconds >= run.budget.max_active_seconds or run.llm_tokens >= run.budget.max_llm_tokens
            or len(run.calls) >= run.budget.max_model_calls):
        return "NONE"
    if run.error_code == "LLM_CONFIGURATION_REQUIRED":
        return "FIX_CONFIGURATION"
    return "CONTINUE"


def validate_resume(run, instant: datetime) -> None:
    if run.retry_not_before and instant < run.retry_not_before:
        raise AgentFailure("LLM_RETRY_NOT_READY")
    if recovery_action(run) == "NONE":
        raise AgentFailure("AGENT_RECOVERY_BUDGET_EXCEEDED")
