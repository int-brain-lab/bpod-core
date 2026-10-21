"""Tests for bpod_core.filtering."""

import re

import pytest

from bpod_core.filtering import match_filter


class TestMatchFilter:
    @pytest.mark.parametrize(
        ('value', 'criterion', 'expected'),
        [
            ('a', None, True),
            (None, None, True),
            ('a', 'a', True),
            ('a', 'b', False),
            (None, 'a', False),
            (1, 1, True),
            ('abc', 'ab', False),
            ('abc', re.compile('^ab'), True),
            ('abc', re.compile('^b'), False),
            (1, re.compile('1'), False),
            (2, [1, 2], True),
            (3, [1, 2], False),
            ('xyz', ['a', re.compile('y')], True),
            ('a', [], False),
            (2, {1, 2}, True),
            ('xyz', frozenset({'a', re.compile('y')}), True),
        ],
    )
    def test_match_filter(self, value, criterion, expected):
        """Values are matched by equality, regex search or any item of a collection."""
        assert match_filter(value, criterion) is expected
