class EngineError(ValueError):
    """Deterministic, data-free error suitable for an application adapter."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)
