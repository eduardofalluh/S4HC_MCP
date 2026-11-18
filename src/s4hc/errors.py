"""The one error type every tool raises, kept apart from client.py so config.py
can raise it without a circular import."""

from __future__ import annotations


class S4HCError(RuntimeError):
    """Application error with a stable code the agent can branch on.

    Codes: INVALID_SCOPE, UNAUTHORIZED_SCOPE, UNSUPPORTED_FIELD, PLAN_UNAVAILABLE,
    QUERY_TOO_LARGE, MIXED_CURRENCY, SAP_UNAUTHORIZED, SAP_FORBIDDEN, SAP_NOT_FOUND,
    SAP_RATE_LIMITED, SAP_UNAVAILABLE, INCOMPLETE_RESULT, METADATA_MISMATCH,
    NOT_CONFIGURED, DISABLED, NOT_FOUND.
    """

    RETRYABLE = {"SAP_RATE_LIMITED", "SAP_UNAVAILABLE"}

    def __init__(self, message: str, code: str = "INVALID_SCOPE") -> None:
        self.code = code
        self.retryable = code in self.RETRYABLE
        super().__init__(f"[{code}] {message}")
