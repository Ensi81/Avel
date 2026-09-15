"""Typed errors for the Interchange SDK — so callers can catch specific,
known outcomes instead of parsing raw HTTP responses."""
from typing import List


class InterchangeError(Exception):
    """Base class for all agent_interchange SDK errors."""


class RequestConflictError(InterchangeError):
    """HTTP 409 from the Interchange: the task_id-derived agent_request_id
    was already used — either for a DIFFERENT transaction (a possible
    replay/hijack of a captured payment proof, rejected outright) or one
    that was reserved but never completed (still in-flight, or the server
    crashed before recording a result — never retried automatically).
    `detail` carries the server's explanation of which."""

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class InsufficientBalanceError(InterchangeError):
    """/pay returned decision=DENY specifically because the payer's
    balance can't cover amount + fee (as opposed to a risk-score DENY)."""

    def __init__(self, reasons: List[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons) or "insufficient balance")


class InterchangeAPIError(InterchangeError):
    """Any other non-2xx response from the Interchange (400, 422, 502, ...)."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Interchange API error {status_code}: {detail}")
