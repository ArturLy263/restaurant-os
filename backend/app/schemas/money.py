"""Monetary values in API responses.

The target contract (docs/monetary-contract.md) is a decimal string with
exactly two decimals, e.g. "12.50". New fields use `Money`; existing
fields keep their current format until the coordinated Stage 2 change.
"""

from decimal import Decimal
from typing import Annotated

import re

from pydantic import BeforeValidator, PlainSerializer, WithJsonSchema


Money = Annotated[
    Decimal,
    PlainSerializer(
        lambda value: f"{value:.2f}",
        return_type=str,
        when_used="json",
    ),
    WithJsonSchema(
        {
            "type": "string",
            "pattern": r"^-?\d{1,8}\.\d{2}$",
            "examples": ["12.50"],
        },
        mode="serialization",
    ),
]


_MONEY_INPUT = re.compile(r"\d{1,8}(\.\d{1,2})?")


def _money_from_string(value):
    if not isinstance(value, str):
        raise ValueError('must be a decimal string, e.g. "12.50"')
    if not _MONEY_INPUT.fullmatch(value):
        raise ValueError(
            'must be a non-negative amount with at most two decimals, e.g. "12.50"'
        )
    return Decimal(value)


# A non-negative amount in a request, accepted only as a decimal string
# ("12.50", "12.5", "12"): JSON numbers are refused. Used by every cashier
# request (docs/monetary-contract.md, decision D).
MoneyInput = Annotated[
    Decimal,
    BeforeValidator(_money_from_string),
    WithJsonSchema(
        {
            "type": "string",
            "pattern": r"^\d{1,8}(\.\d{1,2})?$",
            "examples": ["12.50"],
        },
        mode="validation",
    ),
]
