"""API error contract.

Every error response has the same body:

    {
        "detail": "Order is not open",
        "error": {
            "code": "ORDER_NOT_OPEN",
            "message": "Order is not open",
            "request_id": "6f1c0e0b9a5e4d4f8e0f2c3a1b2c3d4e"
        }
    }

`detail` is kept for backward compatibility (for validation errors it is
still FastAPI's list of field errors). `error.code` is stable and meant
for programs; `error.message` is for humans and may change. The request ID
is also returned in the X-Request-ID header and appears in the server log
for unexpected errors.
"""

from enum import StrEnum

from fastapi import HTTPException


class ErrorCode(StrEnum):
    # Generic, derived from the HTTP status of errors without a specific code.
    BAD_REQUEST = "BAD_REQUEST"
    NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    CONFLICT = "CONFLICT"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    HTTP_ERROR = "HTTP_ERROR"

    # Authentication
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    USER_INACTIVE = "USER_INACTIVE"

    # Authorization
    PERMISSION_DENIED = "PERMISSION_DENIED"
    ORDER_ACCESS_DENIED = "ORDER_ACCESS_DENIED"
    STATION_ACCESS_DENIED = "STATION_ACCESS_DENIED"

    # Missing resources
    TABLE_NOT_FOUND = "TABLE_NOT_FOUND"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    ORDER_ITEM_NOT_FOUND = "ORDER_ITEM_NOT_FOUND"
    PRODUCT_NOT_FOUND = "PRODUCT_NOT_FOUND"
    BILL_NOT_FOUND = "BILL_NOT_FOUND"
    PREPARATION_NOT_FOUND = "PREPARATION_NOT_FOUND"
    STATION_NOT_FOUND = "STATION_NOT_FOUND"

    # Tables and orders
    TABLE_NOT_AVAILABLE = "TABLE_NOT_AVAILABLE"
    TABLE_HAS_ACTIVE_ORDER = "TABLE_HAS_ACTIVE_ORDER"
    ORDER_NOT_OPEN = "ORDER_NOT_OPEN"
    ORDER_CLOSED = "ORDER_CLOSED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    ORDER_HAS_NO_ITEMS = "ORDER_HAS_NO_ITEMS"
    ORDER_HAS_NO_ITEMS_TO_SEND = "ORDER_HAS_NO_ITEMS_TO_SEND"
    ORDER_HAS_ACTIVE_ITEMS = "ORDER_HAS_ACTIVE_ITEMS"
    ORDER_HAS_UNSENT_ITEMS = "ORDER_HAS_UNSENT_ITEMS"
    ORDER_NOT_READY_FOR_BILLING = "ORDER_NOT_READY_FOR_BILLING"
    ORDER_HAS_NO_BILLABLE_ITEMS = "ORDER_HAS_NO_BILLABLE_ITEMS"
    ORDER_BILLED = "ORDER_BILLED"

    # Order items, products and modifiers
    INVALID_QUANTITY = "INVALID_QUANTITY"
    PRODUCT_INACTIVE = "PRODUCT_INACTIVE"
    PRODUCT_HAS_NO_STATION = "PRODUCT_HAS_NO_STATION"
    STATION_INACTIVE = "STATION_INACTIVE"
    MODIFIER_INVALID = "MODIFIER_INVALID"
    MODIFIER_SELECTION_INVALID = "MODIFIER_SELECTION_INVALID"
    ORDER_ITEM_ALREADY_SENT = "ORDER_ITEM_ALREADY_SENT"
    ORDER_ITEM_ALREADY_CANCELLED = "ORDER_ITEM_ALREADY_CANCELLED"
    ORDER_ITEM_NOT_CANCELLABLE = "ORDER_ITEM_NOT_CANCELLABLE"

    # Preparations
    INVALID_PREPARATION_TRANSITION = "INVALID_PREPARATION_TRANSITION"

    # Bills and payments
    BILL_NOT_OPEN = "BILL_NOT_OPEN"
    BILL_HAS_PAYMENTS = "BILL_HAS_PAYMENTS"
    BILL_AMOUNT_DUE = "BILL_AMOUNT_DUE"
    INVALID_PAYMENT_AMOUNT = "INVALID_PAYMENT_AMOUNT"
    PAYMENT_EXCEEDS_AMOUNT_DUE = "PAYMENT_EXCEEDS_AMOUNT_DUE"

    # Idempotency
    IDEMPOTENCY_KEY_INVALID = "IDEMPOTENCY_KEY_INVALID"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"


# Codes for HTTPExceptions raised without one (e.g. by FastAPI itself).
STATUS_ERROR_CODES = {
    400: ErrorCode.BAD_REQUEST,
    401: ErrorCode.NOT_AUTHENTICATED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    409: ErrorCode.CONFLICT,
    422: ErrorCode.VALIDATION_ERROR,
    500: ErrorCode.INTERNAL_ERROR,
}


class ApiError(HTTPException):
    """An HTTPException with a stable machine-readable error code.

    The message becomes both `detail` and `error.message`.
    """

    def __init__(
        self,
        status_code: int,
        code: ErrorCode,
        message: str,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(
            status_code=status_code,
            detail=message,
            headers=headers,
        )
        self.code = code


def error_code_for(exc: HTTPException) -> str:
    code = getattr(exc, "code", None)

    if code is not None:
        return str(code)

    return str(STATUS_ERROR_CODES.get(exc.status_code, ErrorCode.HTTP_ERROR))
