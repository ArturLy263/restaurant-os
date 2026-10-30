"""The Money type: decimal strings with exactly two decimals."""

from decimal import Decimal

import pytest
from pydantic import BaseModel

from app.schemas.money import Money


class Priced(BaseModel):
    price: Money


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("12.5"), "12.50"),
        (Decimal("12.50"), "12.50"),
        (Decimal("0"), "0.00"),
        (Decimal("-3.00"), "-3.00"),
        (Decimal("99999999.99"), "99999999.99"),
    ],
)
def test_money_is_a_two_decimal_string_in_json(value, expected):
    assert Priced(price=value).model_dump(mode="json") == {"price": expected}
    assert Priced(price=value).model_dump_json() == f'{{"price":"{expected}"}}'


def test_money_stays_a_decimal_in_python():
    assert Priced(price=Decimal("1.5")).model_dump() == {"price": Decimal("1.5")}
