"""Tests for the data structures in bpod_core.bpod.structs."""

from collections.abc import Iterator
from typing import get_args

from bpod_core.bpod.structs import (
    BpodEventUnion,
    BpodMessage,
    BpodReplyUnion,
    BpodRequestUnion,
)


def _all_subclasses(cls: type) -> Iterator[type]:
    """Yield all direct and indirect subclasses of a class."""
    for subclass in cls.__subclasses__():
        yield subclass
        yield from _all_subclasses(subclass)


def test_bpod_message_union_is_complete():
    """Union TypeAliases cover every concrete BpodMessage subclass."""
    assert {
        *get_args(BpodRequestUnion),
        *get_args(BpodReplyUnion),
        *get_args(BpodEventUnion),
    } == set(_all_subclasses(BpodMessage))
