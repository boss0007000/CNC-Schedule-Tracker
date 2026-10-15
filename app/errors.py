class UserError(Exception):
    """A validation failure with a clear, user-facing message."""

    def __init__(self, message: str, code: str = "invalid", field: str | None = None, persist: bool = False):
        super().__init__(message)
        self.message = message
        self.code = code
        self.field = field
        self.persist = persist  # True: keep DB writes made before raising (rate-limit hits, no-show marks)


class RateLimited(UserError):
    def __init__(self, message: str = "Too many attempts. Please wait a few minutes and try again."):
        super().__init__(message, code="rate_limited", persist=True)
