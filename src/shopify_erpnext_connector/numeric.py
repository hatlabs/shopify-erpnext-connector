"""Number checks for values parsed from JSON, TOML and stored records.

bool is an int subclass, but `true` is never a quantity or a duration.
Python's JSON parser accepts NaN and Infinity, which are no quantity either.
"""

import math
from typing import TypeGuard


def is_number(value: object) -> TypeGuard[int | float]:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return math.isfinite(value)


def is_integer(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)
