"""Matching values against filter criteria."""

import re
from collections.abc import Collection
from typing import Any, TypeAlias, TypeVar

T = TypeVar('T')
"""Value type variable for :data:`Filter`."""

Filter: TypeAlias = T | re.Pattern[str] | Collection[T | re.Pattern[str]]
"""Criterion accepted by :func:`match_filter`.

A scalar for an exact match, a compiled regular expression for a pattern match on
strings, or a collection of those to match any of its items.
"""


def match_filter(value: object, criterion: Filter[Any] | None) -> bool:
    r"""
    Check whether a value satisfies a filter criterion.

    Parameters
    ----------
    value : object
        The value to check.
    criterion : Filter or None
        The criterion to check against:

        - ``None``: no constraint, any value matches
        - Scalar: exact match
        - :class:`re.Pattern`: regex search, matches strings only
        - Collection: match any item (OR logic); must not contain ``None``

    Returns
    -------
    bool
        True if the value satisfies the criterion, False otherwise.

    Examples
    --------
    >>> match_filter('COM3', None)
    True
    >>> match_filter('COM3', 'COM3')
    True
    >>> match_filter('/dev/ttyACM0', re.compile(r'ttyACM\d+'))
    True
    >>> match_filter(0x16C0, [0x16C0, 0x0483])
    True
    >>> match_filter(None, 'COM3')
    False
    """

    def matches(item: Filter[Any]) -> bool:
        if isinstance(item, Collection) and not isinstance(item, str):
            return any(matches(i) for i in item)
        if isinstance(item, re.Pattern):
            return isinstance(value, str) and item.search(value) is not None
        return bool(value == item)

    return criterion is None or matches(criterion)
