"""How a payment or refund is confirmed: the extension point for payment
terminals and providers.

Stage 3 has no integration. Cash is counted by the cashier; a card is run
on an external bank terminal and the cashier confirms the result by hand
(MANUAL_CARD_TERMINAL). RestOS records that confirmation and who gave it;
it has not verified anything with the bank and never says it has.

A future integration implements `PaymentConfirmer` (e.g. a terminal that
answers asynchronously) and is returned by `confirmer_for`; the bill and
cash domain only store the `Confirmation` it returns. A provider that can
leave a payment pending will add a PENDING payment status (with its
migration) and make shift closing wait for it.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from fastapi import status

from app.core.errors import ApiError, ErrorCode


@dataclass(frozen=True)
class Confirmation:
    # payments.confirmation_method / refunds.confirmation_method
    method: str
    confirmed_at: datetime
    external_reference: str | None


class PaymentConfirmer(Protocol):
    def confirm(
        self,
        *,
        cashier_confirmed: bool,
        external_reference: str | None,
    ) -> Confirmation:
        """Confirm money taken (a payment) or returned (a refund)."""


class CashReceived:
    """Cash: the cashier takes or gives the notes; nothing to confirm."""

    def confirm(self, *, cashier_confirmed: bool, external_reference: str | None) -> Confirmation:
        if external_reference:
            raise ApiError(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                code=ErrorCode.VALIDATION_ERROR,
                message="A cash payment has no terminal reference",
            )
        return Confirmation("CASH_RECEIVED", datetime.utcnow(), None)


class ManualCardTerminal:
    """A card run on an external terminal, confirmed by the cashier."""

    def confirm(self, *, cashier_confirmed: bool, external_reference: str | None) -> Confirmation:
        if not cashier_confirmed:
            raise ApiError(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                code=ErrorCode.CARD_CONFIRMATION_REQUIRED,
                message=(
                    "Confirm that the card terminal approved the transaction "
                    "(card_confirmed: true)"
                ),
            )
        return Confirmation("MANUAL_CARD_TERMINAL", datetime.utcnow(), external_reference or None)


_CONFIRMERS: dict[str, PaymentConfirmer] = {
    "CASH": CashReceived(),
    "CARD": ManualCardTerminal(),
}


def confirmer_for(method: str) -> PaymentConfirmer:
    return _CONFIRMERS[method]


# 12 or more digits, possibly separated by spaces or dashes: what a card
# number looks like. A terminal reference must never hold one.
_CARD_NUMBER_LIKE = re.compile(r"(?:\d[ -]?){12,}")


def check_external_reference(value: str | None) -> str | None:
    """A terminal/transaction reference: trimmed, empty -> None; refuses
    anything that looks like a card number."""

    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if _CARD_NUMBER_LIKE.search(value):
        raise ValueError(
            "looks like a card number; enter the terminal's receipt or "
            "transaction number, never card data"
        )
    return value
