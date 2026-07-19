from __future__ import annotations


class ApplicationError(RuntimeError):
    default_message = "请求处理失败。"
    default_code = "INTERNAL_ERROR"
    default_status_code = 500

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: list[dict[str, str]] | None = None,
        conversation_id: str | None = None,
        task_id: str | None = None,
    ) -> None:
        super().__init__(message or self.default_message)
        self.code = code or self.default_code
        self.status_code = status_code or self.default_status_code
        self.details = list(details or [])
        self.conversation_id = conversation_id
        self.task_id = task_id


class ApplicationValidationError(ApplicationError):
    default_message = "请求字段验证失败。"
    default_code = "VALIDATION_FAILED"
    default_status_code = 422


class ResourceNotFoundError(ApplicationError):
    default_message = "请求的资源不存在。"
    default_code = "RESOURCE_NOT_FOUND"
    default_status_code = 404


class ApplicationConflictError(ApplicationError):
    default_message = "资源状态冲突。"
    default_code = "RESOURCE_CONFLICT"
    default_status_code = 409


class DependencyUnavailableError(ApplicationError):
    default_message = "依赖服务暂不可用。"
    default_code = "DEPENDENCY_UNAVAILABLE"
    default_status_code = 503


class ApplicationInternalError(ApplicationError):
    default_message = "内部处理失败。"
    default_code = "INTERNAL_ERROR"
    default_status_code = 500


class OrchestrationOutcomeError(ApplicationError):
    """A persisted CHAT_ORCHESTRATION outcome projected to public HTTP."""


class InvalidCursorError(ApplicationValidationError):
    default_message = "分页游标无效。"


def from_persistence_error(
    error: Exception,
    *,
    conversation_id: str | None = None,
    task_id: str | None = None,
) -> ApplicationError:
    from materialsagent.domain.ports.unit_of_work import (
        DatabaseUnavailableError,
        PersistenceConflictError,
        PersistenceError,
    )

    if isinstance(error, PersistenceConflictError):
        return ApplicationConflictError(
            conversation_id=conversation_id,
            task_id=task_id,
        )
    if isinstance(error, DatabaseUnavailableError):
        return DependencyUnavailableError(
            conversation_id=conversation_id,
            task_id=task_id,
        )
    if isinstance(error, PersistenceError):
        return ApplicationInternalError(
            conversation_id=conversation_id,
            task_id=task_id,
        )
    return ApplicationInternalError(
        conversation_id=conversation_id,
        task_id=task_id,
    )
