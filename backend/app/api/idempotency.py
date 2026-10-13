"""Idempotency-Key support for critical mutations.

A route opts in by depending on `Idempotency` and running its service call
through `Idempotency.run`:

    def create_payment(..., idempotency: Idempotency = Depends()):
        return idempotency.run(
            lambda: BillService.create_payment(...),
            payload=request,
        )

Contract (also documented in the README):

- The header is optional. Without it the request runs exactly as before,
  with no replay protection.
- Keys are scoped to the authenticated user (and so to the restaurant):
  the same key used by another user is an unrelated key.
- The first request with a key runs the operation. Its key, the business
  change and the response are committed in one transaction, so either all
  of them are stored or none.
- A later request with the same key and the same request (method, route,
  path parameters and body) does not run the operation again; it gets the
  stored status code and body, with `Idempotent-Replayed: true`.
- The same key with a different request is rejected with 422
  IDEMPOTENCY_KEY_REUSED.
- A request whose key is in use by a request still running waits for it
  (PostgreSQL blocks the second insert of the same key) and then replays
  its result, or runs itself if the first one failed.
- Only successful results are stored. If the operation fails (an error
  response or a crash), the transaction is rolled back and nothing is
  stored, so a retry with the same key runs the operation again against
  the current state.
- Stored results are kept for KEY_LIFETIME; afterwards the key may be
  used again as a new key.
"""

import json
import logging
import re
import uuid
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any

from fastapi import Depends, Header, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.core.errors import ApiError, ErrorCode
from app.db.dependencies import get_db
from app.db.models.idempotency_key import IdempotencyKey
from app.db.models.user import User
from app.db.unit_of_work import owned_transaction


logger = logging.getLogger("app.idempotency")

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
KEY_LIFETIME = timedelta(hours=24)

# Printable ASCII without spaces, e.g. a UUID.
_VALID_KEY = re.compile(r"[\x21-\x7e]{1,255}")

# Documents the header and its errors in the OpenAPI schema of each route.
# Declaring 422 replaces FastAPI's default 422 entry, so it repeats that
# entry's HTTPValidationError schema (the shape of validation errors).
IDEMPOTENT_ROUTE_RESPONSES = {
    400: {"description": "Invalid Idempotency-Key (IDEMPOTENCY_KEY_INVALID)"},
    422: {
        "description": (
            "Validation error (VALIDATION_ERROR), or the Idempotency-Key "
            "was already used for a different request "
            "(IDEMPOTENCY_KEY_REUSED)"
        ),
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/HTTPValidationError"},
            },
        },
    },
}


def request_fingerprint(
    operation: str,
    path_params: dict[str, Any],
    payload: Any,
) -> str:
    """SHA-256 of the canonical JSON of the request's identifying parts."""

    canonical = json.dumps(
        _canonical(
            {
                "operation": operation,
                "path_params": path_params,
                "body": payload,
            }
        ),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )

    return sha256(canonical.encode()).hexdigest()


def _canonical(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _canonical(value.model_dump(mode="python"))

    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in value.items()}

    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]

    if isinstance(value, Decimal):
        # 10, 10.0 and 10.00 are the same amount.
        return format(value.normalize(), "f")

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    return str(value)


class Idempotency:
    """Request dependency that runs an operation at most once per key."""

    def __init__(
        self,
        request: Request,
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_user),
        idempotency_key: str | None = Header(
            default=None,
            alias=IDEMPOTENCY_KEY_HEADER,
            description=(
                "Optional, recommended. A unique client-generated value "
                "(e.g. a UUID, 1-255 printable ASCII characters) identifying "
                "this operation; retries with the same key and request "
                "replay the original result instead of repeating it."
            ),
        ),
    ):
        if idempotency_key is not None and not _VALID_KEY.fullmatch(
            idempotency_key
        ):
            raise ApiError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code=ErrorCode.IDEMPOTENCY_KEY_INVALID,
                message=(
                    "Idempotency-Key must be 1-255 printable ASCII "
                    "characters without spaces"
                ),
            )

        self.request = request
        self.db = db
        self.key = idempotency_key
        self.restaurant_id = current_user.restaurant_id
        self.user_id = current_user.id

    def run(
        self,
        operation: Callable[[], Any],
        payload: BaseModel | None = None,
    ) -> Any:
        """Run `operation` (a service call) under the request's key.

        Without a key, returns the operation's result unchanged. With a key,
        returns a JSONResponse with the route's status code and the result
        serialized by the route's response model, either fresh or replayed.
        """

        if self.key is None:
            return operation()

        route = self.request.scope["route"]
        operation_name = f"{self.request.method} {route.path}"
        fingerprint = request_fingerprint(
            operation_name,
            dict(self.request.path_params),
            payload,
        )

        db = self.db

        try:
            with owned_transaction(db) as transaction:
                record_id = self._claim(operation_name, fingerprint)

                if record_id is None:
                    replay = self._replay(fingerprint)
                    # Nothing was written; commit just ends the transaction.
                    transaction.commit()
                    return replay

                result = operation()

                status_code = route.status_code or status.HTTP_200_OK
                body = route.response_model.model_validate(
                    result,
                    from_attributes=True,
                ).model_dump(mode="json", by_alias=True)

                db.execute(
                    update(IdempotencyKey)
                    .where(IdempotencyKey.id == record_id)
                    .values(
                        response_status_code=status_code,
                        response_body=body,
                    )
                )

                # The business change, the key and its response together.
                transaction.commit()
        except BaseException:
            db.rollback()
            raise

        return JSONResponse(content=body, status_code=status_code)

    def _scope(self):
        return (
            IdempotencyKey.restaurant_id == self.restaurant_id,
            IdempotencyKey.user_id == self.user_id,
            IdempotencyKey.idempotency_key == self.key,
        )

    def _claim(self, operation_name: str, fingerprint: str) -> uuid.UUID | None:
        """Insert this request's key; None if the key is already stored.

        If another transaction has inserted the same key and not finished,
        PostgreSQL makes this insert wait for it: after it commits, the key
        is stored (None is returned); after it rolls back, the insert here
        succeeds. Either way the operation never runs twice.
        """

        self.db.execute(
            delete(IdempotencyKey).where(
                *self._scope(),
                IdempotencyKey.expires_at <= func.now(),
            )
        )

        return self.db.scalar(
            insert(IdempotencyKey)
            .values(
                id=uuid.uuid4(),
                restaurant_id=self.restaurant_id,
                user_id=self.user_id,
                idempotency_key=self.key,
                operation=operation_name,
                request_fingerprint=fingerprint,
                expires_at=func.now() + KEY_LIFETIME,
            )
            .on_conflict_do_nothing(
                constraint="uq_idempotency_keys_scope_key",
            )
            .returning(IdempotencyKey.id)
        )

    def _replay(self, fingerprint: str) -> JSONResponse:
        record = self.db.scalar(
            select(IdempotencyKey)
            .where(*self._scope())
            .execution_options(populate_existing=True)
        )

        # Rows are committed only together with their response.
        if record is None or record.response_status_code is None:
            raise RuntimeError(
                "Idempotency key conflicted but no stored result was found"
            )

        if record.request_fingerprint != fingerprint:
            raise ApiError(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                code=ErrorCode.IDEMPOTENCY_KEY_REUSED,
                message=(
                    "Idempotency-Key has already been used for a "
                    "different request"
                ),
            )

        logger.info(
            "Replaying stored result for %s (idempotency record %s)",
            record.operation,
            record.id,
        )

        return JSONResponse(
            content=record.response_body,
            status_code=record.response_status_code,
            headers={REPLAYED_HEADER: "true"},
        )
