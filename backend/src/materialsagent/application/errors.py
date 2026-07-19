from __future__ import annotations


class ApplicationError(RuntimeError):
    default_message = "请求处理失败。"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.default_message)


class ApplicationValidationError(ApplicationError):
    default_message = "请求字段验证失败。"


class ResourceNotFoundError(ApplicationError):
    default_message = "请求的资源不存在。"


class ApplicationConflictError(ApplicationError):
    default_message = "资源状态冲突。"


class DependencyUnavailableError(ApplicationError):
    default_message = "依赖服务暂不可用。"


class ApplicationInternalError(ApplicationError):
    default_message = "内部处理失败。"


class InvalidCursorError(ApplicationValidationError):
    default_message = "分页游标无效。"


def from_persistence_error(error: Exception) -> ApplicationError:
    from materialsagent.domain.ports.unit_of_work import (
        DatabaseUnavailableError,
        PersistenceConflictError,
        PersistenceError,
    )

    if isinstance(error, PersistenceConflictError):
        return ApplicationConflictError()
    if isinstance(error, DatabaseUnavailableError):
        return DependencyUnavailableError()
    if isinstance(error, PersistenceError):
        return ApplicationInternalError()
    return ApplicationInternalError()
