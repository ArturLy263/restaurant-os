"""Monetary values in API responses.

The target contract (docs/monetary-contract.md) is a decimal string with
exactly two decimals, e.g. "12.50". New fields use `Money`; existing
fields keep their current format until the coordinated Stage 2 change.
"""

from decimal import Decimal
from typing import Annotated

from pydantic import PlainSerializer, WithJsonSchema


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
