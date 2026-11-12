import math

import pytest

from shopify_erpnext_connector.numeric import is_integer, is_number


@pytest.mark.parametrize("value", [0, 3, -2, 1.5])
def test_finite_numbers_are_numbers(value: object) -> None:
    assert is_number(value)


@pytest.mark.parametrize(
    "value", [True, False, None, "1", math.nan, math.inf, -math.inf]
)
def test_booleans_strings_and_non_finite_floats_are_not_numbers(value: object) -> None:
    assert not is_number(value)


def test_only_ints_are_integers() -> None:
    assert is_integer(3)
    assert not is_integer(3.0)
    assert not is_integer(True)
